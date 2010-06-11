import os, re

from twisted.python import log, runtime

from buildslave.commands.base import SourceBaseCommand
from buildslave import runprocess
from buildslave.commands import utils
from buildslave.util import remove_userpassword


class Mercurial(SourceBaseCommand):
    """Mercurial specific VC operation. In addition to the arguments
    handled by SourceBaseCommand, this command reads the following keys:

    ['repourl'] (required): the Mercurial repository string
    ['clobberOnBranchChange']: Document me. See ticket #462.
    """

    header = "mercurial operation"

    def setup(self, args):
        SourceBaseCommand.setup(self, args)
        self.vcexe = utils.getCommand("hg")
        self.repourl = args['repourl']
        self.clobberOnBranchChange = args.get('clobberOnBranchChange', True)
        self.sourcedata = "%s\n" % self.repourl
        self.branchType = args.get('branchType', 'dirname')
        self.stdout = ""
        self.stderr = ""
        self.clobbercount = 0 # n times we've clobbered

    def sourcedirIsUpdateable(self):
        return os.path.isdir(os.path.join(self.builder.basedir,
                                          self.srcdir, ".hg"))

    def doVCUpdate(self):
        d = os.path.join(self.builder.basedir, self.srcdir)
        command = [self.vcexe, 'pull', '--verbose', self.repourl]
        c = runprocess.RunProcess(self.builder, command, d,
                         sendRC=False, timeout=self.timeout,
                         maxTime=self.maxTime, keepStdout=True, usePTY=False)
        self.command = c
        d = c.start()
        d.addCallback(self._handleEmptyUpdate)
        d.addCallback(self._update)
        return d

    def _handleEmptyUpdate(self, res):
        if type(res) is int and res == 1:
            if self.command.stdout.find("no changes found") != -1:
                # 'hg pull', when it doesn't have anything to do, exits with
                # rc=1, and there appears to be no way to shut this off. It
                # emits a distinctive message to stdout, though. So catch
                # this and pretend that it completed successfully.
                return 0
        return res

    def doVCFull(self):
        d = os.path.join(self.builder.basedir, self.srcdir)
        command = [self.vcexe, 'clone', '--verbose', '--noupdate']

        # if got revision, clobbering and in dirname, only clone to specific revision
        # (otherwise, do full clone to re-use .hg dir for subsequent builds)
        if self.args.get('revision') and self.mode == 'clobber' and self.branchType == 'dirname':
            command.extend(['--rev', self.args.get('revision')])
        command.extend([self.repourl, d])

        c = runprocess.RunProcess(self.builder, command, self.builder.basedir,
                         sendRC=False, timeout=self.timeout,
                         maxTime=self.maxTime, usePTY=False)
        self.command = c
        cmd1 = c.start()
        cmd1.addCallback(self._update)
        return cmd1

    def _clobber(self, dummy, dirname):
        self.clobbercount += 1

        if self.clobbercount > 3:
            raise Exception, "Too many clobber attempts. Aborting step"

        def _vcfull(res):
            return self.doVCFull()

        c = self.doClobber(dummy, dirname)
        c.addCallback(_vcfull)

        return c

    def _purge(self, dummy, dirname):
        d = os.path.join(self.builder.basedir, self.srcdir)
        purge = [self.vcexe, 'purge', '--all']
        purgeCmd = runprocess.RunProcess(self.builder, purge, d,
                                sendStdout=False, sendStderr=False,
                                keepStdout=True, keepStderr=True, usePTY=False)

        def _clobber(res):
            if res != 0:
                # purge failed, we need to switch to a classic clobber
                msg = "'hg purge' failed: %s\n%s. Clobbering." % (purgeCmd.stdout, purgeCmd.stderr)
                self.sendStatus({'header': msg + "\n"})
                log.msg(msg)

                return self._clobber(dummy, dirname)

            # Purge was a success, then we need to update
            return self._update2(res)

        p = purgeCmd.start()
        p.addCallback(_clobber)
        return p

    def _update(self, res):
        if res != 0:
            return res

        # compare current branch to update
        self.update_branch = self.args.get('branch',  'default')

        d = os.path.join(self.builder.basedir, self.srcdir)
        parentscmd = [self.vcexe, 'identify', '--num', '--branch']
        cmd = runprocess.RunProcess(self.builder, parentscmd, d,
                           sendStdout=False, sendStderr=False,
                           keepStdout=True, keepStderr=True, usePTY=False)

        self.clobber = None

        def _parseIdentify(res):
            if res != 0:
                msg = "'hg identify' failed: %s\n%s" % (cmd.stdout, cmd.stderr)
                self.sendStatus({'header': msg + "\n"})
                log.msg(msg)
                return res

            log.msg('Output: %s' % cmd.stdout)

            match = re.search(r'^(.+) (.+)$', cmd.stdout)
            assert match

            rev = match.group(1)
            current_branch = match.group(2)

            if rev == '-1':
                msg = "Fresh hg repo, don't worry about in-repo branch name"
                log.msg(msg)

            elif self.sourcedirIsPatched():
                self.clobber = self._purge

            elif self.update_branch != current_branch:
                msg = "Working dir is on in-repo branch '%s' and build needs '%s'." % (current_branch, self.update_branch)
                if self.clobberOnBranchChange:
                    msg += ' Cloberring.'
                else:
                    msg += ' Updating.'

                self.sendStatus({'header': msg + "\n"})
                log.msg(msg)

                # Clobbers only if clobberOnBranchChange is set
                if self.clobberOnBranchChange:
                    self.clobber = self._purge

            else:
                msg = "Working dir on same in-repo branch as build (%s)." % (current_branch)
                log.msg(msg)

            return 0

        def _checkRepoURL(res):
            parentscmd = [self.vcexe, 'paths', 'default']
            cmd2 = runprocess.RunProcess(self.builder, parentscmd, d,
                               sendStdout=False, sendStderr=False,
                               keepStdout=True, keepStderr=True, usePTY=False)

            def _parseRepoURL(res):
                if res == 1:
                    if "not found!" == cmd2.stderr.strip():
                        msg = "hg default path not set. Not checking repo url for clobber test"
                        log.msg(msg)
                        return 0
                    else:
                        msg = "'hg paths default' failed: %s\n%s" % (cmd2.stdout, cmd2.stderr)
                        log.msg(msg)
                        return 1

                oldurl = cmd2.stdout.strip()

                log.msg("Repo cloned from: '%s'" % oldurl)

                if runtime.platformType  == 'win32':
                    oldurl = oldurl.lower().replace('\\', '/')
                    repourl = self.repourl.lower().replace('\\', '/')
                else:
                    repourl = self.repourl

                if repourl.startswith('file://'):
                    repourl = repourl.split('file://')[1]
                if oldurl.startswith('file://'):
                    oldurl = oldurl.split('file://')[1]

                oldurl = remove_userpassword(oldurl)
                repourl = remove_userpassword(repourl)

                if oldurl.rstrip('/') != repourl.rstrip('/'):
                    self.clobber = self._clobber
                    msg = "RepoURL changed from '%s' in wc to '%s' in update. Clobbering" % (oldurl, repourl)
                    log.msg(msg)

                return 0

            c = cmd2.start()
            c.addCallback(_parseRepoURL)
            return c

        def _maybeClobber(res):
            if self.clobber:
                msg = "Clobber flag set. Doing clobbering"
                log.msg(msg)

                def _vcfull(res):
                    return self.doVCFull()

                return self.clobber(None, self.srcdir)

            return 0

        c = cmd.start()
        c.addCallback(_parseIdentify)
        c.addCallback(_checkRepoURL)
        c.addCallback(_maybeClobber)
        c.addCallback(self._update2)
        return c

    def _update2(self, res):
        d = os.path.join(self.builder.basedir, self.srcdir)

        updatecmd=[self.vcexe, 'update', '--clean', '--repository', d]
        if self.args.get('revision'):
            updatecmd.extend(['--rev', self.args['revision']])
        else:
            updatecmd.extend(['--rev', self.args.get('branch',  'default')])
        self.command = runprocess.RunProcess(self.builder, updatecmd,
            self.builder.basedir, sendRC=False,
            timeout=self.timeout, maxTime=self.maxTime, usePTY=False)
        return self.command.start()

    def parseGotRevision(self):
        # we use 'hg identify' to find out what we wound up with
        command = [self.vcexe, "identify", "--id", "--debug"] # get full rev id
        c = runprocess.RunProcess(self.builder, command,
                         os.path.join(self.builder.basedir, self.srcdir),
                         environ=self.env,
                         sendStdout=False, sendStderr=False, sendRC=False,
                         keepStdout=True, usePTY=False)
        d = c.start()
        def _parse(res):
            m = re.search(r'^(\w+)', c.stdout)
            return m.group(1)
        d.addCallback(_parse)
        return d
