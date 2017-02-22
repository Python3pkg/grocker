#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) Polyconseil SAS. All rights reserved.

from __future__ import absolute_import, division, print_function, unicode_literals
import argparse
import enum
# pylint: disable=wrong-import-order
import io
import logging
import os
import os.path
import sys
# pylint: enable=wrong-import-order

import yaml

from . import __version__
from . import builders
from . import cleanners
from . import helpers
from . import loggers
from . import utils


class GrockerActions(enum.Enum):
    build_dep = 'dep'  # -> dep
    build_img = 'img'  # -> img
    push_img = 'push'  # -> push
    all = 'build'  # -> dep + img + push


def arg_parser():
    """
    Create an CLI args parser

    Some default value (marked as #precedence) are set to None due to precedence
    order (see utils.parse_config() doc string for more information).
    """
    def file_path_type(x):
        return os.path.abspath(os.path.expanduser(x))

    def file_path_or_none_type(x):
        return file_path_type(x) if x is not None else None

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-c', '--config', action='append', type=file_path_or_none_type, default=[],
        help='Grocker config file',
    )
    parser.add_argument(  # precedence
        '-r', '--runtime', default=None,
        help="runtime used to build and run this image",
    )
    parser.add_argument(  # precedence
        '-e', '--entrypoint-name', default=None,
        help="Docker entrypoint to use to run this image",
    )
    parser.add_argument(  # precedence
        '--volume', action='append', default=[], dest='volumes',
        help="Container storage and configuration area",
    )
    parser.add_argument(  # precedence
        '--port', action='append', default=[], dest='ports',
        help="Port on which a container will listen for connections",
    )
    parser.add_argument(
        '--pip-conf', metavar='<file>', type=file_path_or_none_type, default=None,
        help="pip configuration file used to download dependencies (by default use pip config getter)",
    )
    parser.add_argument(
        '--pip-constraint', metavar='<file>', type=file_path_or_none_type, default=None,
        help="pip constraint file used to download dependencies",
    )
    parser.add_argument(
        '--docker-image-prefix', metavar='<url>',
        help='docker registry or account on Docker official registry to use',
    )
    parser.add_argument(
        '--image-base-name', metavar='<name>',
        help="base name for the image (eg '<docker-image-prefix>/<image-base-name>:<image-version>')",
    )
    parser.add_argument('-n', '--image-name', metavar='<name>', help="name used to tag the build image")
    parser.add_argument(
        'actions', choices=GrockerActions, type=GrockerActions, nargs='+',
        metavar='<action>', help='should be one of {}'.format(', '.join(x.value for x in GrockerActions)),
    )

    parser.add_argument(
        '--result-file', metavar='<filename>', default=None,
        help="yaml file where results (image name, ...) are written",
    )
    parser.add_argument('release', metavar='<release>', help="application to build (you can use version specifier)")

    parser.add_argument('--purge', action=PurgeAction, help="purge Grocker containers, images and volumes")
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('-v', '--verbose', action='store_true', help='verbose mode')

    return parser


class PurgeAction(argparse.Action):
    """Argparse action to purge grocker related docker images"""

    def __init__(self, option_strings, *args, **kwargs):
        super(PurgeAction, self).__init__(
            option_strings=option_strings,
            dest=argparse.SUPPRESS,
            choices=('old', 'old:runner', 'all', 'all:runner'),
        )

    def __call__(self, parser, namespace, values, *args, **kwargs):
        current_version = 'all' in values
        runner = 'runner' in values

        loggers.setup()
        docker_client = utils.docker_get_client()
        cleanners.docker_purge_container(docker_client, current_version=current_version)
        cleanners.docker_purge_volumes(docker_client, current_version=current_version)
        cleanners.docker_purge_images(docker_client, current_version=current_version, runner=runner)
        parser.exit()


def clean_actions(actions):
    if GrockerActions.all in actions:
        actions_without_all = set(GrockerActions) - {GrockerActions.all}
        if actions != [GrockerActions.all]:
            sys.exit(
                "The {all} action already specifies {actions_without_all}.\n"
                "Please use either {all} or any combination of ({actions_without_all}).".format(
                    all=GrockerActions.all.value,
                    actions_without_all=', '.join(sorted(action.value for action in actions_without_all)),
                )
            )
        actions = actions_without_all
    return actions


def main():
    parser = arg_parser()
    args = parser.parse_args()
    config = utils.parse_config(
        args.config,
        runtime=args.runtime,
        entrypoint_name=args.entrypoint_name,
        pip_constraint=args.pip_constraint,
        docker_image_prefix=args.docker_image_prefix,
        image_base_name=args.image_base_name,
        volumes=args.volumes,
        ports=args.ports,
    )

    loggers.setup(verbose=args.verbose)
    logger = logging.getLogger('grocker' if __name__ == '__main__' else __name__)

    args.actions = clean_actions(args.actions)

    if config['runtime'] not in config['system']['runtime']:
        raise RuntimeError('Unknown runtime: %s', config['runtime'])

    docker_client = utils.docker_get_client()
    image_name = args.image_name or utils.default_image_name(config, args.release)
    results = {
        'release': args.release,
        'image': image_name,
    }

    if GrockerActions.build_dep in args.actions:
        logger.info('Compiling dependencies...')
        builders.get_or_build_root_image(docker_client, config)
        compiler = builders.get_or_build_compiler_image(docker_client, config)
        results['compiler_image'] = compiler.tags[0]

        with helpers.pip_conf(pip_conf_path=args.pip_conf) as pip_conf:
            builders.compile_wheels(
                docker_client=docker_client,
                config=config,
                release=args.release,
                pip_conf=pip_conf,
            )

    if GrockerActions.build_img in args.actions:
        logger.info('Building image...')
        root_image = builders.get_or_build_root_image(docker_client, config)
        results['root_image'] = root_image.tags[0]
        builders.build_runner_image(
            docker_client=docker_client,
            config=config,
            name=image_name,
            release=args.release,
        )

    if GrockerActions.push_img in args.actions:
        if not builders.is_prefixed_image(image_name):
            logger.warning('Not pushing any image since the registry is unclear in %s', image_name)
        else:
            logger.info('Pushing image...')
            image = builders.docker_push_image(docker_client, image_name)
            result['hash'] = [x.split('@')[1] for x in image.attrs['RepoDigests']][0]

    if args.result_file:
        result_dir = os.path.dirname(args.result_file)
        if not os.path.exists(result_dir):
            os.makedirs(result_dir)
        with io.open(args.result_file, 'w') as fp:
            yaml.dump(results, fp, indent=True)


if __name__ == '__main__':
    main()
