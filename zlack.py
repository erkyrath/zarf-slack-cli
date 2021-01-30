#!/usr/bin/env python

import sys
import os
import optparse
import traceback
import asyncio
import prompt_toolkit
import prompt_toolkit.patch_stdout
import prompt_toolkit.eventloop

import zlackcli.client

token_file = '.zlack-tokens'
prefs_file = '.zlack-prefs'

zlackdir = os.environ.get('ZLACK_DIR')
if not zlackdir:
    zlackdir = os.environ.get('HOME')

token_path = os.path.join(zlackdir, token_file)
prefs_path = os.path.join(zlackdir, prefs_file)

slack_client_id = os.environ.get('SLACK_CLIENT_ID', None)
slack_client_secret = os.environ.get('SLACK_CLIENT_SECRET', None)

popt = optparse.OptionParser(usage='zlack.py [ OPTIONS ] command...')

popt.add_option('--authport',
                action='store', type=int, dest='auth_port', default=8090,
                help='localhost port to redirect authentication to (default: 8090)')
popt.add_option('--slack-client-id',
                action='store', dest='slack_client_id', default=slack_client_id,
                help='Slack client id (default: $SLACK_CLIENT_ID)')
popt.add_option('--slack-client-secret',
                action='store', dest='slack_client_secret', default=slack_client_secret,
                help='Slack client secret (default: $SLACK_CLIENT_SECRET)')
popt.add_option('--debug-exceptions',
                action='store_true', dest='debug_exceptions',
                help='Display complete stack traces of exceptions')
popt.add_option('--debug-messages',
                action='store_true', dest='debug_messages',
                help='Show all command to and from the server')

(opts, args) = popt.parse_args()

client = None

def exception_handler(loop, ctx):
    """This handles exceptions that turn up in the asyncio module.
    That mostly means timeout errors on the web socket.
    (Exceptions generated within zlackcli are handled within zlackcli.)
    """
    msg = ctx.get('message', 'Unhandled exception')
    exstr = ''
    ex = ctx.get('exception')
    if ex is not None:
        exstr = ' (%s: %s)' % (ex.__class__.__name__, ex)
    print('asyncio: %s%s' % (msg, exstr,))

async def mainloop(client):
    """The main input loop. This prompts for user input and dispatches it
    to the client.

    The client's Slack communication work happens in the background,
    due to the magic of async tasks. (It's not a background *thread*;
    rather, the prompt_toolkit module knows how to yield control back
    to the event loop so that everything can happen cooperatively.)
    """
    await client.open()
    
    # Create a history storage object for the command-line prompt.
    history = prompt_toolkit.history.InMemoryHistory()

    # Set up the prompt session.
    psession = prompt_toolkit.PromptSession(history=history)
    
    # And a callback for generating the right-hand prompt flag
    rprompt_func = lambda:client.ui.display_rprompt(psession)
    
    done = False
    while not done:
        try:
            prompt = client.ui.display_current_channel() + '> '
            with prompt_toolkit.patch_stdout.patch_stdout():
                input = await psession.prompt_async(prompt, rprompt=rprompt_func)
            input = input.rstrip()
            if input:
                client.ui.handle_input(input)
        except KeyboardInterrupt:
            print('<KeyboardInterrupt>')
            done = True
        except EOFError:
            print('<EOFError>')
            done = True
            
    await client.close()

# Begin work.

evloop = asyncio.get_event_loop()
evloop.set_exception_handler(exception_handler)

client = zlackcli.client.ZlackClient(token_path, prefs_path, opts=opts, loop=evloop)

evloop.run_until_complete(mainloop(client))
