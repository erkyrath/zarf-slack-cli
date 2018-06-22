#!/usr/bin/env python

import sys
import os
import optparse
import traceback
import asyncio
import prompt_toolkit

import zlackcli.client

token_file = '.zlack-tokens'
prefs_file = '.zlack-prefs'

token_path = os.path.join(os.environ.get('HOME'), token_file)
token_path = './zh-token' ### for testing

prefs_path = os.path.join(os.environ.get('HOME'), prefs_file)

env_client_id = os.environ.get('ZLACK_CLIENT_ID', None)
env_client_secret = os.environ.get('ZLACK_CLIENT_SECRET', None)

popt = optparse.OptionParser(usage='slack-auth.py [ OPTIONS ] command...')

popt.add_option('--authport',
                action='store', type=int, dest='auth_port', default=8090,
                help='localhost port to redirect authentication to (default: 8090)')
popt.add_option('--clientid',
                action='store', dest='client_id', default=env_client_id,
                help='Slack client id (default: $ZLACK_CLIENT_ID)')
popt.add_option('--clientsecret',
                action='store', dest='client_secret', default=env_client_secret,
                help='Slack client secret (default: $ZLACK_CLIENT_SECRET)')
popt.add_option('--debugexceptions',
                action='store_true', dest='debug_exceptions',
                help='Display complete stack traces of exceptions')

(opts, args) = popt.parse_args()


client = zlackcli.client.ZlackClient(token_path, prefs_path, opts=opts)

async def main():
    await client.open()
    
    # Create a history storage object for the command-line prompt.
    history = prompt_toolkit.history.InMemoryHistory()

    done = False
    while not done:
        try:
            prompt = client.display_current_channel() + '> '
            input = await prompt_toolkit.prompt_async(prompt, history=history, patch_stdout=True)
            input = input.rstrip()
            if input:
                if input == '/auth':
                    client.begin_auth(evloop)
                    continue
                if input == '/connect':
                    list(client.teams.values())[0].rtm_connect(evloop)
                    continue
                if input == '/disconnect':
                    list(client.teams.values())[0].rtm_disconnect(evloop)
                    continue
                print('Got: "' + input + '"')
        except KeyboardInterrupt:
            print('<KeyboardInterrupt>')
            done = True
        except EOFError:
            print('<EOFError>')
            done = True
            
    await client.close()

evloop = asyncio.get_event_loop()
evloop.run_until_complete(main())
