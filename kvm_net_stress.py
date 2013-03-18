#!/usr/bin/env python
# kvm_net_stress.py - Run a network stress test on a KVM guest
# Copyright (C) 2013 Marwan Alsabbagh
# license: BSD, see LICENSE for more details.

import time, datetime, tempfile, argparse, logging.config, urlparse, itertools, subprocess, sys
from path import path as Path  # pip install path.py
from argparse import ArgumentParser, RawDescriptionHelpFormatter


def get_temp(name):
    prefix = 'kvm_net_stress_%s_' % name
    return Path(tempfile.NamedTemporaryFile(prefix=prefix, delete=False).name)


POST_FILE = get_temp('post')
ARG_FILE = get_temp('args')
PING_FILE = get_temp('ping')
LOG_DELAY = 0.5
TIMEOUT = 3.0
LOG_CONFIG = {
    'version': 1,
    'formatters': {
        'main': {
            'format': '%(asctime)s %(levelname)5s - %(message)s',
            'datefmt': '%Y-%m-%d %H:%M:%S',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'main',
        },
        'file': {
            'class': 'logging.FileHandler',
            'formatter': 'main',
            'filename': '/var/log/kvm_net_stress.txt',
        },
    },
    'loggers': {
        '': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
        },
    }
}
EPILOG = """
description:
  This script helps reproduce an issue where a kvm guest's networking
  would get stuck after prolonged usage. During each test run a specified
  amount of data will be uploaded and downloaded to the guest machine over
  http. To create 100M of test data on the guest one could run:

  truncate -s 100M /var/www/data.txt

example usage:
  kvm_net_stress.py http://myguest.local/data.txt myguest --post-size=100M
"""


#taken from http://stackoverflow.com/a/1094933/1699750
def sizeof_fmt(num):
    for x in ['bytes', 'KB', 'MB', 'GB', 'TB']:
        if num < 1024.0:
            return "%3.1f %s" % (num, x)
        num /= 1024.0


def kvm_state(guest):
    lines = subprocess.check_output('virsh list --all', shell=True).strip().splitlines()[2:]
    lines = [i.split(None, 2) for i in lines]
    state = [state for id, name, state in lines if name == guest]
    if state:
        return state[0]
    else:
        return 'COULD NOT DETECT STATE'


def restart_guest(guest, url):
    logging.info('shutdown guest...')
    subprocess.check_output('virsh shutdown %s' % guest, shell=True)
    while True:
        state = kvm_state(guest)
        if state == 'shut off':
            break
        logging.info("waiting for guest shutdown. current state '%s'" % state)
        time.sleep(0.5)
    time.sleep(1)
    logging.info('starting guest...')
    subprocess.check_output('virsh start %s' % guest, shell=True)
    while True:
        logging.info('waiting for webserver...')
        cmd = ['/usr/bin/wget', '-O', '/dev/null', '--timeout=1', Path(url).parent]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate()
        if p.returncode == 0 and "`/dev/null' saved" in err:
            return
        logging.info('wget failed returncode: %s output: %s ' % (p.returncode, err))
        time.sleep(1)


def stress(stats, guest, target, url, rate, wget_files, proc_count):
    ARG_FILE.write_text(' '.join(['--output-file=%s' % i for i in wget_files]))
    last_change = time.time()
    last_size = 0
    rate_arg = '--limit-rate=%s' % rate if rate else ''
    wget = '/usr/bin/wget %s -O /dev/null %s --post-file=%s' % (url, rate_arg, POST_FILE)
    cmd = '/usr/bin/xargs --arg-file=%s -n 1 -P %s %s' % (ARG_FILE, proc_count, wget)
    p = subprocess.Popen(cmd.split())
    while True:
        if p.poll() is not None:
            last_lines = [i.text().strip().splitlines() for i in wget_files]
            last_lines = [i[-1] if i else '' for i in last_lines]
            all_saved = all("`/dev/null' saved" in i for i in last_lines)
            if p.returncode == 0 and all_saved:
                stats['down'] += sum([int(i.split('/')[-1].split(']')[0]) for i in last_lines])
                stats['up'] += POST_FILE.size * proc_count
                stats['completed'] += 1
                downloaded = sizeof_fmt(stats['down'])
            else:
                logging.error('WGET EXIT returncode: %s last_lines: %s' % (p.returncode, last_lines))
                sys.exit('this should never happen')
            return
        size = PING_FILE.size
        ping_failed = PING_FILE.lines() and not PING_FILE.lines()[-1].startswith('64 bytes from %s' % target)
        if (size == last_size and (time.time() - last_change) > TIMEOUT) or ping_failed:
            stats['last_hang'] = time.time()
            stats['hanged'] += 1
            logging.error('#%s NETWORK HANG ping_failed: %s' % (stats['seq'], ping_failed))
            logging.info('killing wget processes ...')
            subprocess.check_output('kill -9 %s' % p.pid, shell=True)
            restart_guest(guest, url)
            return
        if size != last_size:
            last_change = time.time()
            last_size = size
        downloaded = sizeof_fmt(stats['down'])
        uploaded = sizeof_fmt(stats['up'])
        total = datetime.timedelta(seconds=int(time.time() - stats['start']))
        last_hang = datetime.timedelta(seconds=int(time.time() - stats['last_hang']))
        msg = '#%s hanged:%s ok:%s total:%s ' % (stats['seq'], stats['hanged'], stats['completed'], total)
        msg += 'last_hang:%s down:%s up: %s log:%s' % (last_hang, downloaded, uploaded, last_size)
        logging.info(msg)
        time.sleep(LOG_DELAY)


def main():
    """Run a network stress test on a KVM guest."""
    parser = ArgumentParser(description=main.__doc__, epilog=EPILOG, formatter_class=RawDescriptionHelpFormatter)
    parser.add_argument('url')
    parser.add_argument('guest')
    parser.add_argument('--limit-rate', dest='rate')
    parser.add_argument('--post-size', default='100M')
    parser.add_argument('--proc-count', default=10, type=int)
    args = parser.parse_args()
    logging.config.dictConfig(LOG_CONFIG)
    logging.info('STARTING STRESS TEST')
    stats = dict(seq=0, down=0, up=0, last_hang=time.time(), start=time.time(), hanged=0, completed=0)
    target = urlparse.urlparse(args.url).netloc
    subprocess.check_output('truncate -s %s %s' % (args.post_size, POST_FILE), shell=True)
    ping = subprocess.Popen('ping -s 56 %s > %s' % (target, PING_FILE), shell=True)
    wget_files = [get_temp('wget_output') for i in range(args.proc_count)]
    try:
        for seq in itertools.count(1):
            stats['seq'] = seq
            stress(stats, args.guest, target, args.url, args.rate, wget_files, args.proc_count)
    finally:
        for path in [POST_FILE, ARG_FILE, PING_FILE] + wget_files:
            path.remove()
        ping.kill()


if __name__ == '__main__':
    main()
