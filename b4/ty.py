#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2020 by the Linux Foundation
#
__author__ = 'Konstantin Ryabitsev <konstantin@linuxfoundation.org>'

import os
import sys
import b4
import re
import email
import email.message
import json

from string import Template
from email import utils
from pathlib import Path

logger = b4.logger

DEFAULT_PR_TEMPLATE = """
On ${sentdate}, ${fromname} wrote:
${quote}

Merged, thanks!

${summary}

Best regards,
-- 
${signature}
"""

DEFAULT_AM_TEMPLATE = """
On ${sentdate}, ${fromname} wrote:
${quote}

Applied, thanks!

${summary}

Best regards,
-- 
${signature}
"""

# Used to track commits created by current user
MY_COMMITS = None


def git_get_merge_id(gitdir, commit_id):
    # get merge commit id
    args = ['rev-list', '%s..' % commit_id, '--ancestry-path']
    lines = b4.git_get_command_lines(gitdir, args)
    if not len(lines):
        return None
    return lines[-1]


def git_get_rev_diff(gitdir, rev):
    args = ['diff', '%s~..%s' % (rev, rev)]
    return b4.git_run_command(gitdir, args)


def make_reply(reply_template, jsondata):
    body = Template(reply_template).safe_substitute(jsondata)
    # Conform to email standards
    body = body.replace('\n', '\r\n')
    msg = email.message_from_string(body)
    msg['From'] = '%s <%s>' % (jsondata['myname'], jsondata['myemail'])
    allto = utils.getaddresses([jsondata['to']])
    allcc = utils.getaddresses([jsondata['cc']])
    # Remove ourselves and original sender from allto or allcc
    for entry in list(allto):
        if entry[1] == jsondata['myemail'] or entry[1] == jsondata['fromemail']:
            allto.remove(entry)
    for entry in list(allcc):
        if entry[1] == jsondata['myemail'] or entry[1] == jsondata['fromemail']:
            allcc.remove(entry)

    # Add original sender to the To
    allto.append((jsondata['fromname'], jsondata['fromemail']))

    msg['To'] = b4.format_addrs(allto)
    msg['Cc'] = b4.format_addrs(allcc)
    msg['In-Reply-To'] = '<%s>' % jsondata['msgid']
    if len(jsondata['references']):
        msg['References'] = '%s <%s>' % (jsondata['references'], jsondata['msgid'])
    else:
        msg['References'] = '<%s>' % jsondata['msgid']

    if jsondata['subject'].find('Re: ') < 0:
        msg['Subject'] = 'Re: %s' % jsondata['subject']
    else:
        msg['Subject'] = jsondata['subject']

    mydomain = jsondata['myemail'].split('@')[1]
    msg['Message-Id'] = email.utils.make_msgid(idstring='b4-ty', domain=mydomain)
    return msg


def auto_locate_pr(gitdir, jsondata, branch):
    pr_commit_id = jsondata['pr_commit_id']
    logger.debug('Checking %s', jsondata['pr_commit_id'])
    if not b4.git_commit_exists(gitdir, pr_commit_id):
        return None

    onbranches = b4.git_branch_contains(gitdir, pr_commit_id)
    if not len(onbranches):
        logger.debug('%s is not on any branches', pr_commit_id)
        return None
    if branch not in onbranches:
        logger.debug('%s is not on branch %s', pr_commit_id, branch)
        return None

    # Get the merge commit
    merge_commit_id = git_get_merge_id(gitdir, pr_commit_id)
    if not merge_commit_id:
        logger.debug('Could not get a merge commit-id for %s', pr_commit_id)
        return None

    # Check that we are the author of the merge commit
    gitargs = ['show', '--format=%ae', merge_commit_id]
    out = b4.git_get_command_lines(gitdir, gitargs)
    if not out:
        logger.debug('Could not get merge commit author for %s', pr_commit_id)
        return None

    usercfg = b4.get_user_config()
    if usercfg['email'] not in out:
        logger.debug('Merged by a different author, ignoring %s', pr_commit_id)
        logger.debug('Author: %s', out[0])
        return None

    return merge_commit_id


def get_all_commits(gitdir, branch, since='1.week', committer=None):
    global MY_COMMITS
    if MY_COMMITS is not None:
        return MY_COMMITS

    MY_COMMITS = dict()
    if committer is None:
        usercfg = b4.get_user_config()
        committer = usercfg['email']

    gitargs = ['log', '--committer', committer, '--no-abbrev', '--oneline', '--since', since, branch]
    lines = b4.git_get_command_lines(gitdir, gitargs)
    if not len(lines):
        logger.debug('No new commits from the current user --since=%s', since)
        return MY_COMMITS

    logger.info('Found %s of your commits since %s', len(lines), since)
    logger.info('Calculating patch hashes, may take a moment...')
    # Get patch hash of each commit
    for line in lines:
        commit_id, subject = line.split(maxsplit=1)
        ecode, out = git_get_rev_diff(gitdir, commit_id)
        pwhash = b4.LoreMessage.get_patchwork_hash(out)
        logger.debug('phash=%s', pwhash)
        MY_COMMITS[pwhash] = (commit_id, subject)

    return MY_COMMITS


def auto_locate_series(gitdir, jsondata, branch, since='1.week', loose=False):
    commits = get_all_commits(gitdir, branch, since)

    patchids = set(commits.keys())
    # We need to find all of them in the commits
    found = list()
    for patch in jsondata['patches']:
        logger.debug('Checking %s', patch)
        if patch[1] in patchids:
            logger.debug('Found: %s', patch[0])
            found.append(commits[patch[1]])
        elif loose:
            # try to locate by subject
            success = False
            for pwhash, commit in commits.items():
                if commit[1] == patch[0]:
                    found.append(commit)
                    success = True
                    break
            if not success:
                logger.debug('  Failed to find a match for: %s', patch[0])

    if len(found) == len(jsondata['patches']):
        logger.debug('Found all the patches')
        return found

    return None


def read_template(tptfile):
    # bubbles up FileNotFound
    tpt = ''
    with open(tptfile, 'r', encoding='utf-8') as fh:
        for line in fh:
            if len(line) and line[0] == '#':
                continue
            tpt += line
    return tpt


def generate_pr_thanks(gitdir, jsondata):
    config = b4.get_main_config()
    thanks_template = DEFAULT_PR_TEMPLATE
    if config['thanks-pr-template']:
        # Try to load this template instead
        try:
            thanks_template = read_template(config['thanks-pr-template'])
        except FileNotFoundError:
            logger.critical('ERROR: thanks-pr-template says to use %s, but it does not exist',
                            config['thanks-pr-template'])
            sys.exit(2)

    if 'merge_commit_id' not in jsondata:
        merge_commit_id = git_get_merge_id(gitdir, jsondata['pr_commit_id'])
        if not merge_commit_id:
            logger.critical('Could not get merge commit id for %s', jsondata['subject'])
            logger.critical('Was it actually merged?')
            sys.exit(1)
        jsondata['merge_commit_id'] = merge_commit_id
    # Make a summary
    cidmask = config['thanks-commit-url-mask']
    if not cidmask:
        cidmask = 'merge commit: %s'
    jsondata['summary'] = cidmask % jsondata['merge_commit_id']
    msg = make_reply(thanks_template, jsondata)
    return msg


def generate_am_thanks(gitdir, jsondata, branch, since):
    config = b4.get_main_config()
    thanks_template = DEFAULT_AM_TEMPLATE
    if config['thanks-am-template']:
        # Try to load this template instead
        try:
            thanks_template = read_template(config['thanks-am-template'])
        except FileNotFoundError:
            logger.critical('ERROR: thanks-am-template says to use %s, but it does not exist',
                            config['thanks-am-template'])
            sys.exit(2)
    if 'commits' not in jsondata:
        commits = auto_locate_series(gitdir, jsondata, branch, since, loose=True)
    else:
        commits = jsondata['commits']

    if commits is None:
        logger.critical('Could not match all commits for: %s', jsondata['subject'])
        logger.critical('Not thanking for this series')
        return None
    cidmask = config['thanks-commit-url-mask']
    if not cidmask:
        cidmask = 'commit: %s'
    slines = list()
    for commit in commits:
        slines.append('- %s' % commit[1])
        slines.append('  %s' % (cidmask % commit[0]))
    jsondata['summary'] = '\n'.join(slines)

    msg = make_reply(thanks_template, jsondata)
    return msg


def auto_thankanator(cmdargs):
    gitdir = cmdargs.gitdir
    wantbranch = get_wanted_branch(cmdargs)
    logger.info('Auto-thankanating commits in %s', wantbranch)
    tracked = list_tracked()
    if not len(tracked):
        logger.info('Nothing to do')
        sys.exit(0)

    applied = list()
    for jsondata in tracked:
        if 'pr_commit_id' in jsondata:
            # this is a pull request
            merge_commit_id = auto_locate_pr(gitdir, jsondata, wantbranch)
            if merge_commit_id is None:
                continue
            jsondata['merge_commit_id'] = merge_commit_id
        else:
            # This is a patch series
            commits = auto_locate_series(gitdir, jsondata, wantbranch, since=cmdargs.since)
            if commits is None:
                continue
            jsondata['commits'] = commits
        applied.append(jsondata)
        logger.info('  Located: %s', jsondata['subject'])

    if not len(applied):
        logger.info('Nothing to do')
        sys.exit(0)

    logger.info('---')
    send_messages(applied, cmdargs.gitdir, cmdargs.outdir, wantbranch, since=cmdargs.since)
    sys.exit(0)


def send_messages(listing, gitdir, outdir, branch, since='1.week'):
    # Not really sending, but writing them out to be sent on your own
    # We'll probably gain ability to send these once the feature is
    # more mature and we're less likely to mess things up
    datadir = b4.get_data_dir()
    logger.info('Generating %s thank-you letters', len(listing))
    # Check if the outdir exists and if it has any .thanks files in it
    if not os.path.exists(outdir):
        os.mkdir(outdir)

    usercfg = b4.get_user_config()
    # Do we have a .signature file?
    sigfile = os.path.join(str(Path.home()), '.signature')
    if os.path.exists(sigfile):
        with open(sigfile, 'r', encoding='utf-8') as fh:
            signature = fh.read()
    else:
        signature = '%s <%s>' % (usercfg['name'], usercfg['email'])

    outgoing = 0
    for jsondata in listing:
        slug_from = re.sub(r'\W', '_', jsondata['fromemail'])
        slug_subj = re.sub(r'\W', '_', jsondata['subject'])
        slug = '%s_%s' % (slug_from.lower(), slug_subj.lower())
        slug = re.sub(r'_+', '_', slug)
        jsondata['myname'] = usercfg['name']
        jsondata['myemail'] = usercfg['email']
        jsondata['signature'] = signature
        if 'pr_commit_id' in jsondata:
            # This is a pull request
            msg = generate_pr_thanks(gitdir, jsondata)
        else:
            # This is a patch series
            msg = generate_am_thanks(gitdir, jsondata, branch, since)

        if msg is None:
            continue

        outgoing += 1
        outfile = os.path.join(outdir, '%s.thanks' % slug)
        logger.info('  Writing: %s', outfile)
        bout = msg.as_string(policy=b4.emlpolicy)
        with open(outfile, 'wb') as fh:
            fh.write(bout.encode('utf-8'))
        logger.debug('Cleaning up: %s', jsondata['trackfile'])
        fullpath = os.path.join(datadir, jsondata['trackfile'])
        os.rename(fullpath, '%s.sent' % fullpath)
    logger.info('---')
    if not outgoing:
        logger.info('No thanks necessary.')
        return

    logger.debug('Wrote %s thank-you letters', outgoing)
    logger.info('You can now run:')
    logger.info('  git send-email %s/*.thanks', outdir)


def list_tracked():
    # find all tracked bits
    tracked = list()
    datadir = b4.get_data_dir()
    paths = sorted(Path(datadir).iterdir(), key=os.path.getmtime)
    for fullpath in paths:
        if fullpath.suffix not in ('.pr', '.am'):
            continue
        with fullpath.open('r', encoding='utf-8') as fh:
            jsondata = json.load(fh)
            jsondata['trackfile'] = fullpath.name
            if fullpath.suffix == '.pr':
                jsondata['pr_commit_id'] = fullpath.stem
        tracked.append(jsondata)
    return tracked


def write_tracked(tracked):
    counter = 1
    config = b4.get_main_config()
    logger.info('Currently tracking:')
    for entry in tracked:
        logger.info('%3d: %s', counter, entry['subject'])
        logger.info('       From: %s <%s>', entry['fromname'], entry['fromemail'])
        logger.info('       Date: %s', entry['sentdate'])
        logger.info('       Link: %s', config['linkmask'] % entry['msgid'])
        counter += 1


def send_selected(cmdargs):
    tracked = list_tracked()
    if not len(tracked):
        logger.info('Nothing to do')
        sys.exit(0)

    if 'all' in cmdargs.send:
        listing = tracked
    else:
        listing = list()
        for num in cmdargs.send:
            try:
                index = int(num) - 1
                listing.append(tracked[index])
            except ValueError:
                logger.critical('Please provide the number of the message')
                logger.info('---')
                write_tracked(tracked)
                sys.exit(1)
            except IndexError:
                logger.critical('Invalid index: %s', num)
                logger.info('---')
                write_tracked(tracked)
                sys.exit(1)
    if not len(listing):
        logger.info('Nothing to do')
        sys.exit(0)

    wantbranch = get_wanted_branch(cmdargs)
    send_messages(listing, cmdargs.gitdir, cmdargs.outdir, wantbranch, cmdargs.since)
    sys.exit(0)


def discard_selected(cmdargs):
    tracked = list_tracked()
    if not len(tracked):
        logger.info('Nothing to do')
        sys.exit(0)

    if 'all' in cmdargs.discard:
        listing = tracked
    else:
        listing = list()
        for num in cmdargs.discard:
            try:
                index = int(num) - 1
                listing.append(tracked[index])
            except ValueError:
                logger.critical('Please provide the number of the message')
                logger.info('---')
                write_tracked(tracked)
                sys.exit(1)
            except IndexError:
                logger.critical('Invalid index: %s', num)
                logger.info('---')
                write_tracked(tracked)
                sys.exit(1)

    if not len(listing):
        logger.info('Nothing to do')
        sys.exit(0)

    datadir = b4.get_data_dir()
    logger.info('Discarding %s messages', len(listing))
    for jsondata in listing:
        fullpath = os.path.join(datadir, jsondata['trackfile'])
        os.rename(fullpath, '%s.discarded' % fullpath)
        logger.info('  Discarded: %s', jsondata['subject'])

    sys.exit(0)


def check_stale_thanks(outdir):
    if os.path.exists(outdir):
        for entry in Path(outdir).iterdir():
            if entry.suffix == '.thanks':
                logger.critical('ERROR: Found existing .thanks files in: %s', outdir)
                logger.critical('       Please send them first (or delete if already sent).')
                logger.critical('       Refusing to run to avoid potential confusion.')
                sys.exit(1)


def get_wanted_branch(cmdargs):
    gitdir = cmdargs.gitdir
    if not cmdargs.branch:
        # Find out our current branch
        gitargs = ['rev-parse', '--abbrev-ref', 'HEAD']
        ecode, out = b4.git_run_command(gitdir, gitargs)
        if ecode > 0:
            logger.critical('Not able to get current branch (git branch --show-current)')
            sys.exit(1)
        wantbranch = out.strip()
        logger.debug('will check branch=%s', wantbranch)
    else:
        # Make sure it's a real branch
        gitargs = ['branch', '--format=%(refname:short)', '--list']
        lines = b4.git_get_command_lines(gitdir, gitargs)
        if not len(lines):
            logger.critical('Not able to get a list of branches (git branch --list)')
            sys.exit(1)
        if cmdargs.branch not in lines:
            logger.critical('Requested branch %s not found in git branch --list', cmdargs.branch)
            sys.exit(1)
        wantbranch = cmdargs.branch

    return wantbranch


def main(cmdargs):
    usercfg = b4.get_user_config()
    if 'email' not in usercfg:
        logger.critical('Please set user.email in gitconfig to use this feature.')
        sys.exit(1)

    if cmdargs.auto:
        check_stale_thanks(cmdargs.outdir)
        auto_thankanator(cmdargs)
    elif cmdargs.send:
        check_stale_thanks(cmdargs.outdir)
        send_selected(cmdargs)
    elif cmdargs.discard:
        discard_selected(cmdargs)
    else:
        tracked = list_tracked()
        if not len(tracked):
            logger.info('No thanks necessary.')
            sys.exit(0)
        write_tracked(tracked)
        logger.info('---')
        logger.info('You can send them using:')
        logger.info('  b4 ty -s 1 [2 3 ...]')