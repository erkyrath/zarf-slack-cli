import random
import urllib
import aiohttp
import aiohttp.web

auth_url = 'https://discord.com/api/oauth2/authorize'

def construct_auth_url(authport, clientid):
    """Construct the URL which the user will use for authentication.
    Returns (slackurl, redirecturl, statestring).
    - slackurl: the URL which the user should enter into a browser.
    - redirecturl: the localhost URL which Slack will send the user back to
      after authenticating.
    - statestring: used to double-check the user's reply when it comes
      back.
    """
    redirecturl = 'http://localhost:%d/' % (authport,)
    statecheck = 'state_%d' % (random.randrange(1000000),)

    params = [
        ('client_id', clientid),
        ('scope', 'identify guilds messages.read'),
        ('redirect_uri', redirecturl),
        ('response_type', 'code'),
        ('state', statecheck),
    ]
    queryls = [ '%s=%s' % (key, urllib.parse.quote(val)) for (key, val) in params ]
    tup = list(urllib.parse.urlparse(auth_url))
    tup[4] = '&'.join(queryls)
    slackurl = urllib.parse.urlunparse(tup)
    
    return (slackurl, redirecturl, statecheck)

def construct_auth_handler(future, statecheck):
    """Construct a handler for aiohttp.web.Server.
    This handler accepts the web request on port 8090. When a valid
    request is received, it sets a value in the passed-in future.
    """
    
    async def handler(request):
        map = request.query
        message = '???'

        if 'code' not in map:
            message = 'No code found.'
        elif 'state' not in map:
            message = 'No state field found.'
        elif map['state'] != statecheck:
            message = 'State field did not match.'
        else:
            code = map['code']
            future.set_result(code)
            message = 'Auth code received: %s\n' % (code,)
        
        return aiohttp.web.Response(text=message)
    
    return handler
