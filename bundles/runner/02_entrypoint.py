#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) Polyconseil SAS. All rights reserved.

"""This is our entry point script which is ran into our container
    on startup.

    It's meant to:

        * prepare the configuration of the container
        * run the correct SERVICE (for django projects)
        * run a command and its parameters
        * start cron jobs
"""
from __future__ import absolute_import, division, print_function, unicode_literals
import argparse
import ConfigParser as configparser
import ctypes
import logging
import logging.config
import os
import re
import shutil
import signal
import socket
import string
import subprocess
import textwrap


BLUE_HOME = '/home/blue'
CONFIG_MOUNT_POINT = '/config'
SSH_CONFIG_DIR = 'ssh-dir'
DJANGO_SETTINGS_PATH = os.path.join(BLUE_HOME, 'app_config')
ENV_CONFIG = os.path.join(BLUE_HOME, 'etc', 'config.env')
LOG_DIR = os.path.join(BLUE_HOME, 'logs')
RUN_DIR = os.path.join(BLUE_HOME, 'run')
SCRIPT_DIR = '/scripts'
TEMPLATE_PATH = os.path.join(BLUE_HOME, 'templates')
VENV = os.path.join(BLUE_HOME, 'app')


def templatize(filename, destination_path, context):
    filename = os.path.join(TEMPLATE_PATH, filename)
    with open(os.path.expanduser(destination_path), 'w') as fw:
        with open(filename) as fh:
            fw.write(string.Template(fh.read()).substitute(context))


class ChildSubReaper(object):
    """
    Wait for all children to terminate context manager
    """
    libc = ctypes.CDLL('libc.so.6')
    PR_SET_CHILD_SUBREAPER = 36

    def __enter__(self):
        self.prctl_set_child_sub_reaper()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.wait_for_children()
        self.prctl_set_child_sub_reaper(False)

    def prctl_set_child_sub_reaper(self, activate=True):
        self.libc.prctl(ctypes.c_int(self.PR_SET_CHILD_SUBREAPER), ctypes.c_ulong(1 if activate else 0))

    @staticmethod
    def wait_for_children():
        logger = logging.getLogger(__name__)
        while True:
            try:
                pid, status = os.wait()
                logger.info('Process #%s terminated with status %s', pid, status)
            except OSError:
                logger.info('All children were terminated.')
                break


class GraceFulShutdown(object):
    SIGTERM = 15

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

        self.process = None
        self.old_handler = None

    def graceful_shutdown_handler(self, signal_code, _frame):
        if signal_code == self.SIGTERM:
            self.process.terminate()

    def __enter__(self):
        self.old_handler = signal.getsignal(self.SIGTERM)
        signal.signal(self.SIGTERM, self.graceful_shutdown_handler)

        self.process = subprocess.Popen(*self.args, **self.kwargs)
        return self.process

    def __exit__(self, exc_type, exc_val, exc_tb):
        signal.signal(self.SIGTERM, self.old_handler)


def get_host_ip():
    output = subprocess.check_output(['ip', 'route', 'show', '0.0.0.0/0'])
    matched = re.search(r'via\s([0-9.]+)\s', output)
    return matched.groups()[0]


def create_directory(path):
    if not os.path.exists(path):
        os.makedirs(path)
    else:
        logging.getLogger(__name__).info("%s already exists !", path)


def execute(*args, **kwargs):
    """Run a command"""
    logging.getLogger(__name__).info("-> running %s", ' '.join(args))
    try:
        subprocess.check_call(args, **kwargs)
    except subprocess.CalledProcessError:
        pass


def get_context():  # TODO: replace by ad-hoc context in function ?
    # uwsgi specific plugin name
    python_version = os.environ['PYTHON_VERSION']
    python_major = python_version.split('.')[0]
    uwsgi_plugin_name = 'python'
    try:
        uwsgi_workers = int(os.environ.get('UWSGI_WORKERS', 10))
    except ValueError:
        uwsgi_workers = 10
    rsyslog_host = os.environ.get('RSYSLOG_HOST', get_host_ip())  # TODO: user input validation. import ipaddress ?
    if python_major == '3':
        uwsgi_plugin_name = '{}{}'.format(uwsgi_plugin_name, python_major)

    return {
        'django_config_path': DJANGO_SETTINGS_PATH,
        'python_version': os.environ['PYTHON_VERSION'],
        'project_name': os.environ['PROJECT_NAME'],
        'project_name_upper': os.environ['PROJECT_NAME'].upper(),
        'uwsgi_plugin_name': uwsgi_plugin_name,
        'uwsgi_workers': uwsgi_workers,
        'rsyslog_host': rsyslog_host,
        'venv': VENV,
        'http_proxy': os.environ.get('http_proxy', ''),
        'https_proxy': os.environ.get('https_proxy', ''),
        'no_proxy': os.environ.get('no_proxy', ''),
    }


def setup_environment():
    regex = re.compile("^([a-zA-Z_1-9]*)=(.*)$")
    with open(ENV_CONFIG) as fh:
        for line in fh.readlines():
            if line.strip() == '':
                continue
            r = regex.search(line)
            if r:
                key, value = r.groups()
                os.environ[key] = value
            else:
                logging.getLogger(__name__).info("> line '%s' is not an environment variable" % line)

    os.environ.update({
        'PATH': '{0}{1}{2}'.format(os.path.join(VENV, 'bin'), os.pathsep, os.environ['PATH']),
        'DJANGO_SETTINGS_MODULE': '{0}.settings'.format(os.environ['PROJECT_NAME']),
        '{0}_CONFIG'.format(os.environ['PROJECT_NAME'].upper()): os.path.join(DJANGO_SETTINGS_PATH, '*.ini'),
    })


def setup_cron(grocker_config):
    pkg_crontab = subprocess.check_output([
        os.path.expanduser('~/app/bin/python'), '-c',
        'import os, re, sys;'
        'from pkg_resources import resource_string, resource_exists;'
        'package_name = re.search("^([\w_]+)", os.environ["PACKAGE_NAME"]).group(0);'
        'resource_tuple = package_name, "crontab";'
        'py3 = sys.version_info[0] == 3;'
        'resource = resource_string(*resource_tuple) if resource_exists(*resource_tuple) else None;'
        'print(resource.decode() if py3 and resource else resource or "");'
    ])

    cron_wrapper_path = os.path.expanduser('~/bin/cronwrapper.sh')
    prepared_crontab = (
        pkg_crontab
        .replace(' www-data ', ' ')
        .replace(' /usr/share/bluesys-cronwrapper/bin/cronwrapper.sh ', ' {0} '.format(cron_wrapper_path))
    )
    mail_block_tpl = textwrap.dedent("""
    MAILFROM={mailfrom}
    MAILTO={mailto}
    PATH={path}
    """)

    cron_env_path = os.path.expanduser(os.path.join('~', 'etc', 'cron.env'))
    templatize('cron.env', cron_env_path, get_context())
    with open(cron_env_path) as f:
        cron_env = f.read()

    crontab = (
        mail_block_tpl.format(
            mailfrom=grocker_config.get('cron', 'mailfrom'),
            mailto=grocker_config.get('cron', 'mailto'),
            path=os.environ['PATH'],
        ) +
        cron_env +
        prepared_crontab
    )

    process = subprocess.Popen(['crontab', '-'], stdin=subprocess.PIPE)
    process.communicate(crontab)
    if process.returncode:
        logging.getLogger(__name__).error("-> crontab returned %d\n%s", process.returncode, crontab)


def setup_app():
    """Take care of django SI configuration:
        * moves configuration from /config/SI/*ini to $HOME/django_settings/
    """
    for path in (DJANGO_SETTINGS_PATH, LOG_DIR, RUN_DIR):
        create_directory(path)

    templatize('settings.ini', os.path.join(DJANGO_SETTINGS_PATH, '50_base_settings.ini'), get_context())

    si_mounted_config_dir = os.path.join(CONFIG_MOUNT_POINT, 'app')
    if not os.path.exists(si_mounted_config_dir):
        logging.getLogger(__name__).warning("No 'app' directory found")
        return

    for filename in os.listdir(si_mounted_config_dir):
        shutil.copy(
            src=os.path.join(si_mounted_config_dir, filename),
            dst=os.path.join(DJANGO_SETTINGS_PATH, filename),
        )


def setup_ssh():
    # Copy ssh-known-hosts
    dst_ssh_dir = os.path.join(BLUE_HOME, '.ssh')
    src_ssh_dir = os.path.join(CONFIG_MOUNT_POINT, SSH_CONFIG_DIR)

    create_directory(dst_ssh_dir)
    os.chmod(dst_ssh_dir, 0o700)
    if os.path.exists(src_ssh_dir):
        for name in os.listdir(src_ssh_dir):
            src = os.path.join(src_ssh_dir, name)
            dst = os.path.join(dst_ssh_dir, name)
            if os.path.isfile(src):
                shutil.copy(src=src, dst=dst)
                os.chmod(dst, 0o600)


def setup_mail_relay(grocker_config):
    context = {
        'smtp_server': grocker_config.get('smtp', 'server'),
    }

    templatize('ssmtp.conf', os.path.join('~', 'etc', 'ssmtp.conf'), context)
    templatize('django_smtp_settings.ini', os.path.join(DJANGO_SETTINGS_PATH, '40_smtp_settings.ini'), context)


def setup_logging(enable_colors):
    colors = {'begin': '\033[1;33m', 'end': '\033[0m'}
    if not enable_colors:
        colors = {'begin': '', 'end': ''}

    logging.config.dictConfig({
        'version': 1,
        'formatters': {
            'simple': {
                'format': '{begin}%(message)s{end}'.format(**colors)
            },
        },
        'handlers': {
            'console': {'class': 'logging.StreamHandler', 'formatter': 'simple'},
        },
        'loggers': {
            __name__: {'handlers': ['console'], 'level': 'INFO'},
        },
    })


def run_shell(*args):
    """Runs a script that HAS to begin with a shebang"""
    args = list(args)
    script_path = os.path.join(SCRIPT_DIR, args[0])
    if os.path.exists(script_path):
        args[0] = script_path

    logging.getLogger(__name__).info("running %s", " ".join(args))
    process = subprocess.Popen(args, cwd=SCRIPT_DIR)
    process.wait()


def start_service(command, *args):
    """Run the corresponding service with uwsgi & nginx"""
    context = get_context()
    context['service'] = args[0]
    templatize('uwsgi.ini', os.path.join('~', 'etc', 'uwsgi.ini'), context)
    templatize('nginx.conf', os.path.join('~', 'etc', 'nginx.conf'), context)

    supervisord_config = os.path.expanduser(os.path.join('~', 'etc', 'supervisord.conf'))
    templatize('supervisord.conf', supervisord_config, context)

    execute('supervisord', '-c', supervisord_config)


def run_cron(command, *args):
    cron_daemon_env = {'TZ': os.environ.get('GROCKER_CRON_TZ', 'UTC')}
    cron_daemon_cmd = ['sudo', 'cron', '-f']
    with ChildSubReaper():
        logging.getLogger(__name__).info("-> running cron daemon...")
        with GraceFulShutdown(cron_daemon_cmd, env=cron_daemon_env) as p:
            p.wait()


def dispatch(args):
    service_mapping = {
        'cron': run_cron,
        'start': start_service,
    }

    args = args or ['/bin/bash']
    fn = service_mapping.get(args[0], run_shell)
    fn(*args)


def get_version():
    with open(os.path.expanduser('~/.grocker_version')) as f:
        return f.read().strip()


def get_grocker_config():
    config = configparser.ConfigParser()
    config.add_section('instance')
    config.set('instance', 'fqdn', socket.getfqdn())

    config.add_section('smtp')
    config.set('smtp', 'server', get_host_ip())

    config.add_section('cron')
    config.set('cron', 'mailfrom', 'cron@example.com')
    config.set('cron', 'mailto', 'nobody@example.com')

    config.read(os.path.join(CONFIG_MOUNT_POINT, 'grocker.ini'))

    return config


def main():
    parser = argparse.ArgumentParser(prog='entrypoint', description='Docker entry point')
    parser.add_argument('--disable-colors', help='disable colors')
    parser.add_argument('--grocker-version', action='version', version=get_version())
    parser.add_argument('args', nargs='*', help='the command and its arguments')
    args = parser.parse_args()

    grocker_config = get_grocker_config()

    setup_logging(not args.disable_colors)
    setup_environment()
    setup_ssh()
    setup_app()
    setup_mail_relay(grocker_config)
    setup_cron(grocker_config)

    dispatch(args.args)

if __name__ == '__main__':
    main()
