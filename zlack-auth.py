#!/usr/bin/env python3

import sys
import os
import os.path
import random
import time
import json
from collections import OrderedDict
import optparse
import urllib.parse
import urllib.request
import http.server
from slackclient import SlackClient

token_file = '.zlack-tokens'
auth_url = 'https://slack.com/oauth/authorize'
env_client_id = os.environ.get('ZLACK_CLIENT_ID', None)
env_client_secret = os.environ.get('ZLACK_CLIENT_SECRET', None)

popt = optparse.OptionParser(usage='slack-auth.py [ OPTIONS ] command...')

popt.add_option('-p', '--port',
                action='store', type=int, dest='port', default=8090,
                help='localhost port to redirect to (default: 8090)')
popt.add_option('--id',
                action='store', dest='client_id', default=env_client_id,
                help='Slack client id (default: $ZLACK_CLIENT_ID)')
popt.add_option('--secret',
                action='store', dest='client_secret', default=env_client_secret,
                help='Slack client secret (default: $ZLACK_CLIENT_SECRET)')

(opts, args) = popt.parse_args()


def read_tokens():
    path = os.path.join(os.environ.get('HOME'), token_file)
    try:
        fl = open(path)
        dat = json.load(fl, object_pairs_hook=OrderedDict)
        fl.close()
        return dat
    except:
        return OrderedDict()

def write_tokens():
    path = os.path.join(os.environ.get('HOME'), token_file)
    fl = open(path, 'w')
    json.dump(tokens, fl, indent=1)
    fl.write('\n')
    fl.close()
    os.chmod(path, 0o700)
    
def print_auth_url():
    statecheck = 'state_%d' % (random.randrange(1000000),)
    redirecturl = 'http://localhost:%d/' % (opts.port,)

    params = [
        ('client_id', opts.client_id),
        ('scope', 'team:read channels:read'),
        ('redirect_uri', redirecturl),
        ('state', statecheck),
    ]
    queryls = [ '%s=%s' % (key, urllib.parse.quote(val)) for (key, val) in params ]
    tup = list(urllib.parse.urlparse(auth_url))
    tup[4] = '&'.join(queryls)
    url = urllib.parse.urlunparse(tup)
    print('Visit this URL to authenticate with Slack:')
    print()
    print(url)
    print()
    
    return (statecheck, redirecturl)

class RedirectHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        tup = urllib.parse.urlparse(self.path)
        map = urllib.parse.parse_qs(tup[4])
        message = '???'
        code = None
        if 'code' not in map or not len(map['code']):
            message = 'No code found.'
        elif 'state' not in map or not len(map['state']):
            message = 'No state field found.'
        elif map['state'][0] != self.server.z_state_check:
            message = 'State field did not match.'
        else:
            code = map['code'][0]
            message = 'Auth code received: %s\n' % (code,)
        self.send_response(200, 'OK')
        self.end_headers()
        self.wfile.write(message.encode())

        self.server.z_got_response = code

def perform_auth():
    (statecheck, url) = print_auth_url()

    print('Waiting for response on %s ...' % (url,))
    server_address = ('localhost', opts.port)
    httpd = http.server.HTTPServer(server_address, RedirectHandler)
    httpd.z_state_check = statecheck
    httpd.z_got_response = False
    while not httpd.z_got_response:
        httpd.handle_request()
    httpd.server_close()

    auth_code = httpd.z_got_response
    print('Got response.')

    # We have the temporary authorization code. Now we exchange it for
    # a permanent access token.
    cli = SlackClient('')
    res = cli.api_call('oauth.access', client_id=opts.client_id, client_secret=opts.client_secret, code=auth_code)

    if not res.get('ok'):
        print('oauth.access call failed: %s' % (res.get('error'),))
        return
    if not res.get('team_id'):
        print('oauth.access response had no team_id')
        return
    teamid = res.get('team_id')
    
    team = OrderedDict()
    for key in ('team_id', 'team_name', 'user_id', 'scope', 'access_token'):
        if key in res:
            team[key] = res.get(key)

    # Done.
    tokens[teamid] = team
    write_tokens()

# Begin work.

random.seed()

if not opts.client_id:
    print('You must supply the --id argument or set $ZLACK_CLIENT_ID')
    sys.exit(-1)

tokens = read_tokens()

if not args:
    command = 'list'
else:
    command = args.pop(0)

if command == 'list':
    print('Currently authenticated for:')
    for team in tokens.values():
        teamname = team.get('team_name', '???')
        username = team.get('user_name', '???')
        print(' %s (%s)' % (teamname, username,))
elif command in ('login', 'auth'):
    perform_auth()
else:
    print('Commands: list login logout')
