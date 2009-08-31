#!/usr/bin/env python

import optparse, os, subprocess, signal, socket, sys
from itertools import chain

class DiscoSettings(dict):
    defaults = {
        'DISCO_FLAGS':          "''",
        'DISCO_LOG_DIR':        "'/var/log/disco'",
        'DISCO_MASTER_HOME':    "'/usr/lib/disco'",
        'DISCO_MASTER_PORT':    "DISCO_PORT",
        'DISCO_NAME':           "'disco_%s' % DISCO_SCGI_PORT",
        'DISCO_PID_DIR':        "'/var/run'",
        'DISCO_PORT':           "8989",
        'DISCO_ROOT':           "'/srv/disco'",
        'DISCO_SCGI_PORT':      "4444",
        'DISCO_ULIMIT':         "16000000",
        'DISCO_USER':           "os.getenv('LOGNAME')",
        'DISCO_DATA':           "os.path.join(DISCO_ROOT, 'data')",
        'DISCO_MASTER_ROOT':    "os.path.join(DISCO_DATA, '_%s' % DISCO_NAME)",
        'DISCO_CONFIG':         "os.path.join(DISCO_ROOT, '%s.config' % DISCO_NAME)",
        'DISCO_LOCAL_DIR':      "os.path.join(DISCO_ROOT, 'local', '_%s' % DISCO_NAME)",
        'DISCO_WORKER':         "os.path.join(DISCO_HOME, 'node', 'disco-worker')",
        'DISCO_ERLANG':         "guess_erlang()",
        'DISCO_HTTPD':          "'lighttpd'",
        'DISCO_WWW_ROOT':       "os.path.join(DISCO_MASTER_HOME, 'www')",
        'PYTHONPATH':           "'%s:%s/pydisco' % (os.getenv('PYTHONPATH', ''), DISCO_HOME)",
        }

    must_exist = ('DISCO_DATA', 'DISCO_ROOT',
                  'DISCO_MASTER_HOME', 'DISCO_MASTER_ROOT',
                  'DISCO_LOG_DIR', 'DISCO_PID_DIR')

    def __init__(self, filename, **kwargs):
        super(DiscoSettings, self).__init__(kwargs)
        execfile(filename, {}, self)

    def __getitem__(self, key):
        if key not in self:
            return eval(self.defaults[key], globals(), self)
        return super(DiscoSettings, self).__getitem__(key)

    @property
    def env(self):
        settings = os.environ.copy()
        settings.update(dict((k, str(self[k])) for k in self.defaults))
        return settings

class DiscoError(Exception):
    pass

class server(object):
    def __init__(self, disco_settings, port=None):
        self.disco_settings = disco_settings
        self.host = socket.gethostname()
        self.port = port

    @property
    def env(self):
        return self.disco_settings.env

    @property
    def id(self):
        return self.__class__.__name__, self.host, self.port

    @property
    def pid(self):
        return int(open(self.pid_file).readline().strip())

    @property
    def log_file(self):
        return os.path.join(self.disco_settings['DISCO_LOG_DIR'], '%s-%s_%s.log' % self.id)

    @property
    def pid_file(self):
        return os.path.join(self.disco_settings['DISCO_PID_DIR'], '%s-%s_%s.pid' % self.id)

    def conf_path(self, filename):
        return os.path.join(self.disco_settings['DISCO_CONF'], filename)

    def restart(self):
        return chain(self.stop(), self.start())

    def send(self, command):
        return getattr(self, command)()

    def start(self, args=None, **kwargs):
        self.assert_status('stopped')
        if not args:
            args = self.args
        process = subprocess.Popen(args, env=self.env, **kwargs)
        if process.wait():
            raise DiscoError("Failed to start %s" % self)
        yield '%s started' % self
    
    def assert_status(self, status):
        if self._status != status:
            raise DiscoError("%s already %s" % (self, self._status))

    @property
    def _status(self):
        try:
            os.getpgid(self.pid)
            return 'running'
        except Exception:
            return 'stopped'

    def status(self):
        yield '%s %s' % (self, self._status)

    def stop(self):
        self.assert_status('running')
        try:
            os.kill(self.pid, signal.SIGTERM)
            while self._status == 'running':
                pass
        except Exception:
            pass
        for msg in chain(self.status()):
            yield msg

    def __str__(self):
        return ' '.join(self.args)

class lighttpd(server):
    def __init__(self, disco_settings, port, config_file):
        super(lighttpd, self).__init__(disco_settings, port)
        self.config_file = config_file

    @property
    def args(self):
        return [self.disco_settings['DISCO_HTTPD'], '-f', self.config_file]

    @property
    def env(self):
        env = self.disco_settings.env
        env.update({'DISCO_HTTP_LOG': self.log_file,
                    'DISCO_HTTP_PID': self.pid_file,
                    'DISCO_HTTP_PORT': str(self.port)})
        return env
        
class master(server):
    def __init__(self, disco_settings):
        super(master, self).__init__(disco_settings, disco_settings['DISCO_SCGI_PORT'])

    @property
    def args(self):
        return self.basic_args + ['-detached',
                                  '-heart',
                                  '-kernel', 'error_logger', '{file, "%s"}' % self.log_file]

    @property
    def basic_args(self):
        settings = self.disco_settings
        return settings['DISCO_ERLANG'].split() + \
               ['+K', 'true',
                '-rsh', 'ssh',
                '-connect_all', 'false',
                '-pa', os.path.join(settings['DISCO_MASTER_HOME'], 'ebin'),
                '-sname', self.name,
                '-disco', 'disco_name', '"%s"' % settings['DISCO_NAME'],
                '-disco', 'disco_root', '"%s"' % settings['DISCO_MASTER_ROOT'],
                '-disco', 'scgi_port', '%s' % settings['DISCO_SCGI_PORT'],
                '-disco', 'disco_localdir', '"%s"' % settings['DISCO_LOCAL_DIR'],
                '-eval', '[handle_job, handle_ctrl]',
                '-eval', 'application:start(disco)']

    @property
    def env(self):
        env = self.disco_settings.env
        env.update({'DISCO_MASTER_PID': self.pid_file})
        return env
    
    @property
    def lighttpd(self):
        return lighttpd(self.disco_settings,
                        self.disco_settings['DISCO_MASTER_PORT'],
                        self.conf_path('lighttpd-master.conf'))

    @property
    def name(self):
        return '%s_master' % self.disco_settings['DISCO_NAME']

    @property
    def nodename(self):
        return '%s@%s' % (self.name, self.host.split('.', 1)[0])

    def nodaemon(self):
        return chain(self.lighttpd.start(),
                     ('' for x in self.start(self.basic_args)), # suppress output
                     self.lighttpd.stop())

    def send(self, command):
        if command in ('nodaemon', 'remsh'):
            return getattr(self, command)()
        return chain(getattr(self, command)(), self.lighttpd.send(command))
    
    def __str__(self):
        return 'disco master'

class worker(server):
    @property
    def lighttpd(self):
        return lighttpd(self.disco_settings,
                        self.disco_settings['DISCO_PORT'],
                        self.conf_path('lighttpd-worker.conf'))

    @property
    def name(self):
        return '%s_slave' % self.disco_settings['DISCO_NAME']

    def send(self, command):
        return self.lighttpd.send(command)

class debug(object):
    def __init__(self, disco_settings):
        self.disco_settings = disco_settings

    @property
    def name(self):
        return '%s_remsh' % os.getpid()

    def send(self, command):
        discomaster = master(self.disco_settings)
        nodename = discomaster.nodename
        if command != 'status':
            nodename = '%s@%s' % (discomaster.name, command)
        args = self.disco_settings['DISCO_ERLANG'].split() + ['-remsh', nodename,
                                                        '-sname', self.name]
        if subprocess.Popen(args).wait():
            raise DiscoError("Could not connect to %s (%s)" % (command, nodename))
        yield 'closing remote shell to %s (%s)' % (command, nodename)

def guess_erlang():
    if os.uname()[0] == 'Darwin':
        return '/usr/libexec/StartupItemContext erl'
    return 'erl'
    
def main():
    DISCO_BIN  = os.path.dirname(os.path.realpath(__file__))
    DISCO_HOME = os.path.dirname(DISCO_BIN)
    DISCO_CONF = os.path.join(DISCO_HOME, 'conf')

    usage = """            
            %prog [options] [master|worker] [start|stop|restart|status]
            %prog [options] master nodaemon
            %prog [options] debug [hostname]
            """
    option_parser = optparse.OptionParser(usage=usage)
    option_parser.add_option('-s', '--settings',
                             default=os.path.join(DISCO_CONF, 'settings.py'),
                             help='use settings file settings')
    option_parser.add_option('-v', '--verbose',
                             action='store_true',
                             help='print debugging messages')
    option_parser.add_option('-p', '--print-env',
                             action='store_true',
                             help='print the parsed disco environment and exit')
    options, sys.argv = option_parser.parse_args()

    disco_settings = DiscoSettings(options.settings,
                                   DISCO_BIN=DISCO_BIN,
                                   DISCO_HOME=DISCO_HOME,
                                   DISCO_CONF=DISCO_CONF)

    if options.verbose:
        print(
            """
            It seems that Disco is at {DISCO_HOME}
            Disco settings are at {0}
            
            If this is not what you want, see the `--help` option
            """.format(options.settings, **disco_settings)) # python2.6+

    for name in disco_settings.must_exist:
        path = disco_settings[name]
        if not os.path.exists(path):
            os.makedirs(path)

    if options.print_env:
        for item in sorted(disco_settings.env.iteritems()):
            print('%s = %s' % (item))
        sys.exit(0)
            
    argdict      = dict(enumerate(sys.argv))
    disco_object = globals()[argdict.pop(0, 'master')](disco_settings)

    command = argdict.pop(1, 'status')
    for message in disco_object.send(command):
        if options.verbose or command == 'status':
            print(message)

signal.signal(signal.SIGINT, signal.SIG_IGN)

if __name__ == '__main__':
    try:
        main()
    except DiscoError, e:
        sys.exit(e)
    except Exception, e:
        print('Disco encountered a fatal system error:')
        sys.exit(e)