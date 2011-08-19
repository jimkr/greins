import glob
import inspect
import os.path
import sys
import textwrap
import threading
import time
import traceback

from gunicorn.app.wsgiapp import WSGIApplication
from gunicorn.config import make_settings

from greins.reloader import Reloader
from greins.router import Router

class GreinsApplication(WSGIApplication):

    def init(self, parser, opts, args):
        if len(args) != 1:
            parser.error("No configuration directory specified.")
        if not os.path.isdir(args[0]):
            parser.error("APP_DIR must refer to an existing directory.")

        self.cfg.set("default_proc_name", parser.get_prog_name())
        self.app_dir = os.path.abspath(args[0])
        self._use_reloader = opts.reloader

        self._mounts = {}
        self._hooks = {}

        """
        Set up server hook proxies

        Rather than explicitly referring to defined Gunicorn server hooks,
        which may change in future versions of Gunicorn, take configuration
        settings from gunicorn.config.make_settings().

        For each setting in the "Server Hooks" category, create a proxy
        function (with matching arity in order to pass validation), which
        calls the hook for every loaded app that defines it.
        """

        hook_proxy_template = textwrap.dedent(
        """
        def proxy%(spec)s:
            for handler in greins._hooks[name]['handlers']:
                handler%(spec)s
        """)

        for name, obj in make_settings().items():
            if obj.section == "Server Hooks":
                self._hooks[name] = {
                    "handlers": [],
                    "validator": obj.validator
                }
                # Grab the arg spec from the default handler
                spec = inspect.formatargspec(*inspect.getargspec(obj.default))
                # Make an environment to build and capture the proxy
                proxy_env = {
                    "greins": self,
                    "name": name
                }
                # Create the proxy
                exec hook_proxy_template % {'spec': spec} in proxy_env
                self.cfg.set(name, proxy_env['proxy'])

    def load(self):
        if self._use_reloader:
            reloader = Reloader()

        threads = []
        for cf in glob.glob(os.path.join(self.app_dir, '*.py')):
            # isolate config loads on different threads (or greenlets if
            # this is a gevent worker).  If one of the apps fails to
            # start cleanly, the other apps will still function
            # properly.
            t = FileLoader(cf, self._use_reloader, self.logger)
            threads.append(t)
            t.start()

        # wait up to approximately 5 seconds for threads to finish
        max_wait = 5
        start_time = time.time()
        while [t for t in threads if t.isAlive()] and (time.time() - start_time) < max_wait :
            time.sleep(0.01)

        for t in threads:
            # thread finished successfully
            if not t.is_alive():
                # pull the loaded mount points out of the thread
                for route, value in t.mounts.iteritems():
                    if self._mounts.setdefault(route, value) != value:
                        self.logger.warning("Duplicate routes for '%s'", route)

                # Set up server hooks
                for hook in self._hooks:
                    handler = t.cfg.get('%s' % hook)
                    if handler:
                        try:
                            self._hooks[hook]['validator'](handler)
                            self._hooks[hook]['handlers'].append(handler)
                        except Exception, e:
                            self.logger.exception("Error setting up hooks for file %s", t.cf)

                if self._use_reloader:
                    reloader.extra_files.union(t.reloader_extra_files)
            # thread didn't finish.  not much we can do except ignore it
            else:
                self.logger.error("file failed to load: %s", t.cf())

        if self._use_reloader:
            reloader.start()
        router = Router(mounts=self._mounts)
        self.logger.info("Greins booted successfully.")
        self.logger.debug("Routes:\n%s" % router)
        return router

    def _do_hook(self, name, argtuple):
        for handler in self._hooks[name]['handlers']:
            handler(*argtuple)


class FileLoader(threading.Thread):
    def __init__(self, cf, use_reloader, logger):
        threading.Thread.__init__(self)

		# config file to read from
        self.cf = cf
        self._use_reloader = use_reloader
		# collect list of files to reload, if using reloader
        self.reloader_extra_files = set()
        self.logger = logger
		# collect mounts specified in config file
        self.mounts = {}
        self.cfg = {
            "__builtins__": __builtins__,
            "__name__": "__config__",
            "__file__": os.path.abspath(self.cf),
            "__doc__": None,
            "__package__": None,
            "mounts": {}
        }

    def run(self):
        cf_name = os.path.splitext(os.path.basename(self.cf))[0]
        try:
            """
            Read an app configuration from a greins config file.
            Files should contain app handlers with mount points
            for one or more wsgi applications.

            The handlers will be executed inside the environment
            created by the configuration file.
            """
            self.logger.info("Loading configuration for %s" % cf_name)
            execfile(self.cf, self.cfg, self.cfg)

            # Load all the mount points
            for r, a in self.cfg['mounts'].iteritems():
                # Capture the handler in a closure
                def wrap(app):
                    def app_with_env(env, start_response):
                        return app(env, start_response)
                    app_with_env.__name__ = app.__name__
                    return app_with_env
                wrapped = wrap(a)
                if not r.startswith('/'):
                    self.logger.warning("Adding leading '/' to '%s'" % r)
                    r = '/' + r
                if self.mounts.setdefault(r, wrapped) != wrapped:
                    self.logger.warning("Duplicate routes for '%s'" % r)
                    continue

        except Exception, e:
            if self._use_reloader:
                for fname, _, _, _ in traceback.extract_tb(sys.exc_info()[2]):
                     self.reloader_extra_files.add(fname)
                if isinstance(e, SyntaxError):
                     self.reloader_extra_files.add(e.filename)
            self.logger.exception("Exception reading config for %s:" % \
                                      cf_name)

def run():
    """\
    The ``greins`` command line runner for launching Gunicorn with
    a greins configuration directory.
    """
    from greins.app import GreinsApplication
    GreinsApplication("%prog [OPTIONS] APP_DIR").run()
