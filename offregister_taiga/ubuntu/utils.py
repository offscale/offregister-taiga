from itertools import ifilterfalse, imap
from operator import is_
from functools import partial
from os import path
from pkg_resources import resource_filename

from fabric.api import run, cd, put, sudo, prefix, shell_env

from offregister_fab_utils.apt import skip_apt_update, apt_depends, download_and_install
from offregister_web_servers.circus.ubuntu import install as install_circus

taiga_dir = partial(path.join, path.dirname(resource_filename('offregister_taiga', '__init__.py')), 'data')


def install_python_taiga_deps(cloned_xor_updated):
    remote_home = run('printf $HOME')
    apt_depends(
        'python3', 'python3-pip', 'python-dev', 'python3-dev', 'python-pip', 'libzmq3-dev',
        'virtualenvwrapper', 'libxml2-dev', 'libxslt1-dev', 'gettext', 'libgettextpo-dev'
    )

    if not run("dpkg-query --showformat='${Version}' --show python3-lxml") == '3.5.0-1':
        download_and_install(url_prefix='https://launchpad.net/ubuntu/+source/lxml/3.5.0-1/+build/8393479/+files/',
                             packages=('python3-lxml_3.5.0-1_amd64.deb',))

    with shell_env(WORKON_HOME=run('printf $HOME/.virtualenvs')), prefix(
        'source /usr/share/virtualenvwrapper/virtualenvwrapper.sh'):
        mkvirtualenv_if_needed_factory('-p /usr/bin/python3.4 --system-site-packages')('taiga')

        with prefix('workon taiga'), cd('taiga-back'):
            run("sed -i '0,/lxml==3.5.0b1/s//lxml==3.5.0/' requirements.txt")
            run('pip install -r requirements.txt')

            if cloned_xor_updated == 'cloned':
                put(taiga_dir('django.settings.py'), 'settings/local.py')
                run('python manage.py migrate --noinput')
                run('python manage.py compilemessages')
                run('python manage.py collectstatic --noinput')
                run('python manage.py loaddata initial_user')
                run('python manage.py loaddata initial_project_templates')
                run('python manage.py loaddata initial_role')
                run('python manage.py sample_data')
                install_circus(template_vars={'HOME': remote_home, 'USER': run('printf $USER')},
                               local_tpl_dir=taiga_dir())
            else:
                run('python manage.py migrate --noinput')
                run('python manage.py compilemessages')
                run('python manage.py collectstatic --noinput')
                sudo('service circus restart')


def mkvirtualenv_if_needed_factory(extra_args='-p /usr/bin/python3.4'):
    def mkvirtualenv_if_needed(*env_names):
        return tuple(ifilterfalse(partial(is_, False),
                                  imap(lambda env_name: run(
                                      "lsvirtualenv | grep -q '{env_name}'".format(env_name=env_name),
                                      warn_only=True, quiet=True).failed and run(
                                      "mkvirtualenv '{env_name}' {extra_args}".format(
                                          env_name=env_name, extra_args=extra_args), warn_only=True) and env_name,
                                       env_names)))

    return mkvirtualenv_if_needed
