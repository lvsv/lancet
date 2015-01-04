import os
import click
import keyring
import configparser
import pygit2

from . import __version__
from .settings import load_config, USER_CONFIG
from .git import SlugBranchGetter
from .base import Lancet, WarnIntegrationHelper, ShellIntegrationHelper
from .utils import taskstatus


CONTEXT_SETTINGS = dict(help_option_names=['-h', '--help'])


def get_issue(lancet, key=None):
    with taskstatus('Looking up issue on the issue tracker') as ts:
        issue = lancet.get_issue(key)
        summary = issue.fields.summary
        crop = len(summary) > 40
        if crop:
            summary = summary[:40] + '...'
        ts.ok('Retrieved issue {}: {}'.format(issue.key, summary))
    return issue


def get_transition(ctx, lancet, issue, to_status):
    current_status = issue.fields.status.name
    if current_status != to_status:
        transitions = [t['id'] for t in lancet.tracker.transitions(issue)
                       if t['to']['name'] == to_status]
        if not transitions:
            click.secho(
                'No transition from "{}" to "{}" found, aborting.'
                .format(current_status, to_status),
                fg='red', bold=True
            )
            ctx.exit(1)
        elif len(transitions) > 1:
            click.secho(
                'Multiple transitions found from "{}" to "{}", aborting.'
                .format(current_status, to_status),
                fg='red', bold=True
            )
            ctx.exit(1)
        else:
            transition_id = transitions[0]
    else:
        transition_id = None
    return transition_id


def assign_issue(lancet, issue, username, active_status=None):
    with taskstatus('Assigning issue to you') as ts:
        assignee = issue.fields.assignee
        if not assignee or assignee.key != username:
            if issue.fields.status.name == active_status:
                ts.abort('Issue already active and not assigned to you')
            else:
                lancet.tracker.assign_issue(issue, username)
                ts.ok('Issue assigned to you')
        else:
            ts.ok('Issue already assigned to you')


def set_issue_status(lancet, issue, to_status, transition):
    with taskstatus('Setting issue status to "{}"'.format(to_status)) as ts:
        if transition is not None:
            lancet.tracker.transition_issue(issue, transition)
            ts.ok('Issue status set to "{}"'.format(to_status))
        else:
            ts.ok('Issue already "{}"'.format(to_status))


def setup_helper(ctx, param, value):
    if not value or ctx.resilient_parsing:
        return
    base = os.path.abspath(os.path.dirname(__file__))
    helper = os.path.join(base, 'helper.sh')
    with open(helper) as fh:
        click.echo(fh.read())
    ctx.exit()


@click.group(context_settings=CONTEXT_SETTINGS)
@click.version_option(version=__version__, message='%(prog)s %(version)s')
@click.option('--setup-helper', callback=setup_helper, is_flag=True,
              expose_value=False, is_eager=True,
              help='Print the shell integration code and exit.')
@click.pass_context
def main(ctx):
    # TODO: Remove me once not needed anymore
    import warnings
    warnings.simplefilter('ignore', ImportWarning, 2150)

    # TODO: Enable this using a command line switch
    # import logging
    # logging.basicConfig(level=logging.DEBUG)

    try:
        integration_helper = ShellIntegrationHelper(
            os.environ['LANCET_SHELL_HELPER'])
    except KeyError:
        integration_helper = WarnIntegrationHelper()

    ctx.obj = Lancet(load_config(), integration_helper)
    ctx.obj.call_on_close = ctx.call_on_close

    ctx.call_on_close(integration_helper.close)


@click.command()
@click.option('--base', '-b', 'base_branch')
@click.argument('issue')
@click.pass_context
def workon(ctx, issue, base_branch):
    """
    Start work on a given issue.

    This command retrieves the issue from the issue tracker, creates and checks
    out a new aptly-named branch, puts the issue in the configured active,
    status, assigns it to you and starts a correctly linked Harvest timer.

    If a branch with the same name as the one to be created already exists, it
    is checked out instead. Variations in the branch name occuring after the
    issue ID are accounted for and the branch renamed to match the new issue
    summary.

    If the `default_project` directive is correctly configured, it is enough to
    give the issue ID (instead of the full project prefix + issue ID).
    """
    lancet = ctx.obj

    username = lancet.config.get('tracker', 'username')
    if not base_branch:
        base_branch = lancet.config.get('repository', 'base_branch')
    remote_name = lancet.config.get('repository', 'remote_name')
    remote_username = lancet.config.get('repository', 'remote_username')
    active_status = lancet.config.get('tracker', 'active_status')

    credentials = pygit2.KeypairFromAgent(remote_username)

    branch_getter = SlugBranchGetter(base_branch, credentials, remote_name)

    # Get the issue
    issue = get_issue(lancet, issue)

    # Get the working branch
    branch = branch_getter(lancet.repo, issue)

    # Make sure the issue is in a correct status
    transition = get_transition(ctx, lancet, issue, active_status)

    # Make sure the issue is assigned to us
    assign_issue(lancet, issue, username, active_status)

    # Activate environment
    set_issue_status(lancet, issue, active_status, transition)

    with taskstatus('Checking out working branch') as ts:
        lancet.repo.checkout(branch.name)
        ts.ok('Checked out working branch based on "{}"'.format(base_branch))

    with taskstatus('Starting harvest timer') as ts:
        lancet.timer.start(issue)
        ts.ok('Started harvest timer')

main.add_command(workon)


@click.command()
@click.argument('issue')
@click.pass_obj
def time(lancet, issue):
    """
    Start an Harvest timer for the given issue.

    This command takes care of linking the timer with the issue tracker page
    for the given issue.
    """
    issue = get_issue(lancet, issue)

    with taskstatus('Starting harvest timer') as ts:
        lancet.timer.start(issue)
        ts.ok('Started harvest timer')

main.add_command(time)


@click.command()
@click.pass_context
def pause(ctx):
    """
    Pause work on the current issue.

    This command puts the issue in the configured paused status and stops the
    current Harvest timer.
    """
    lancet = ctx.obj
    paused_status = lancet.config.get('tracker', 'paused_status')

    # Get the issue
    issue = get_issue(lancet)

    # Make sure the issue is in a correct status
    transition = get_transition(ctx, lancet, issue, paused_status)

    # Activate environment
    set_issue_status(lancet, issue, paused_status, transition)

    with taskstatus('Pausing harvest timer') as ts:
        lancet.timer.pause()
        ts.ok('Harvest timer paused')

main.add_command(pause)


@click.command()
@click.pass_context
def resume(ctx):
    """
    Resume work on the currently active issue.

    The issue is retrieved from the currently active branch name.
    """
    lancet = ctx.obj

    username = lancet.config.get('tracker', 'username')
    active_status = lancet.config.get('tracker', 'active_status')

    # Get the issue
    issue = get_issue(lancet)

    # Make sure the issue is in a correct status
    transition = get_transition(ctx, lancet, issue, active_status)

    # Make sure the issue is assigned to us
    assign_issue(lancet, issue, username, active_status)

    # Activate environment
    set_issue_status(lancet, issue, active_status, transition)

    with taskstatus('Resuming harvest timer') as ts:
        lancet.timer.start(issue)
        ts.ok('Resumed harvest timer')

main.add_command(resume)


@click.command()
@click.argument('issue', required=False)
@click.pass_obj
def browse(lancet, issue):
    """
    Open the issue tracker page for the given issue in your default browser.

    If no issue is provided, the one linked to the current branch is assumed.
    """
    click.launch(get_issue(lancet, issue).permalink())

main.add_command(browse)


@click.command()
@click.option('-f', '--force/--no-force', default=False)
@click.pass_context
def setup(ctx, force):
    """
    Run a wizard to create the user-level configuration file.
    """
    if os.path.exists(USER_CONFIG) and not force:
        click.secho(
            'An existing configuration file was found at "{}".\n'
            .format(USER_CONFIG),
            fg='red', bold=True
        )
        click.secho(
            'Please remove it before in order to run the setup wizard or use\n'
            'the --force flag to overwrite it.'
        )
        ctx.exit(1)

    tracker_url = click.prompt('URL of the issue tracker')
    tracker_user = click.prompt('Username for {}'.format(tracker_url))
    timer_url = click.prompt('URL of the time tracker')
    timer_user = click.prompt('Username for {}'.format(timer_url))

    config = configparser.ConfigParser()

    config.add_section('tracker')
    config.set('tracker', 'url', tracker_url)
    config.set('tracker', 'username', tracker_user)

    config.add_section('harvest')
    config.set('harvest', 'url', timer_url)
    config.set('harvest', 'username', timer_user)

    with open(USER_CONFIG, 'w') as fh:
        config.write(fh)

    click.secho('\nConfiguration correctly written to "{}".'
                .format(USER_CONFIG), fg='green')

main.add_command(setup)


@click.command()
@click.pass_obj
def logout(lancet):
    """
    Forget saved passwords for the web services.
    """
    services = ['tracker', 'harvest']

    for service in services:
        url = lancet.config.get(service, 'url')
        key = 'lancet+{}'.format(url)
        username = lancet.config.get(service, 'username')
        with taskstatus('Logging out from {}', url) as ts:
            if keyring.get_password(key, username):
                keyring.delete_password(key, username)
                ts.ok('Logged out from {}', url)
            else:
                ts.ok('Already logged out from {}', url)

main.add_command(logout)


# TODO:
# * init (project)
# * pullrequest
#     push
#     pull-request
#     update JIRA issue (transition/assign/comment)
#     stop timer
# * review
#     pull
#     ci-status
#     pep8
#     diff
#     mergeability (rebase is of the submitter responsibility)
# * merge
#     pull, merge, delete
# * issues
#     list all open/assigned issues (or by filter)
# * comment
#     adds a comment to the currently active issue
