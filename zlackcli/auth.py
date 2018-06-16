import random
import urllib

auth_url = 'https://slack.com/oauth/authorize'

def construct_auth_url():
    """Construct the URL which the user will use for authentication.
    Returns (slackurl, redirecturl, statestring).
    - slackurl: the URL which the user should enter into a browser.
    - redirecturl: the localhost URL which Slack will send the user back to
      after authenticating.
    - statestring: used to double-check the user's reply when it comes
      back.
    """
    redirecturl = 'http://localhost:%d/' % (opts.port,)
    statecheck = 'state_%d' % (random.randrange(1000000),)

    params = [
        ('client_id', opts.client_id),
        ('scope', 'client'),
        ('redirect_uri', redirecturl),
        ('state', statecheck),
    ]
    queryls = [ '%s=%s' % (key, urllib.parse.quote(val)) for (key, val) in params ]
    tup = list(urllib.parse.urlparse(auth_url))
    tup[4] = '&'.join(queryls)
    slackurl = urllib.parse.urlunparse(tup)
    
    return (slackurl, redirecturl, statecheck)

