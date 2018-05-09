# Zlack: a minimalist Slack command-line client

- Created by Andrew Plotkin <erkyrath@eblong.com>
- [MIT license][license]

[license]: ./LICENSE
[slackpost]: http://blog.zarfhome.com/2018/03/open-letter-slack-should-not.html

As you probably know, Slack is turning off their IRC and XMPP gateways on May 15th. (I have [written an objection to this][slackpost], but it didn't help.) This project is a replacement for those gateways.

This is *not* a full-on Slack client! It's a replacement for the way I used the XMPP gateway: a little window in the corner of my screen where I can keep an eye on a few important Slack groups and type quick replies. Plain text only. It doesn't try to handle every fancy new feature that Slack rolls out. If you want to do something fancy, you pull up the official Slack client or the web page.

## Installation

This tool is written in Python3. You'll need a recent version of that. You'll also need two packages:

> `pip3 install slackclient`
> `pip3 install prompt-toolkit`

(This is written to [slackclient 1.2.1][slackclient] and [prompt-toolkit 1.0.15][prompt-toolkit]. Both toolkits are undergoing active development and future major revisions may not work the same, so keep an eye out for changes.)

[slackclient]: https://github.com/slackapi/python-slackclient
[prompt-toolkit]: https://github.com/jonathanslenders/python-prompt-toolkit

You'll also have to create your own Slack app client ID. This repository doesn't include any such ID, because Slack doesn't want them to be publicized.

Visit [Slack's developer page][slackapp] and create a new app. Then, under "Permissions", add `http://localhost:8090/` as a redirect URL.

[slackapp]: https://api.slack.com/apps

Once you've done this, set the `ZLACK_CLIENT_ID` and `ZLACK_CLIENT_SECRET` environment variables to the values shown on your "App Credentials" page. 

## Authentication

Run `zlack-auth.py` to authenticate. (The environment variables must be set.)

> `python3 zlack-auth.py login`

This will displays a Slack URL to visit. It also starts listening on localhost port 8090. Authorize the client at Slack, and then you will be redirected back to the localhost port. Once this succeeds, your authentication token will be written into `~/.zlack-tokens`.

## Running the Client

Now you can run `zlack-client.py`. (This does not need the environment variables.) 

> `python3 zlack-client.py`

To send a message, type

> *\#team/channel* Message text to send.

Once you send a message on a channel, you can use this shortcut:

> *\#channel* Message sent to a channel of the most recently-used team.

If your input does not begin with a `#` sign, the message will be sent to the most recently-used channel.

## Work in progress

I haven't written any documentation because I haven't settled on how it works yet. Use at your own risk. Actually, don't use it at all. I can't answer questions about it right now.

I'll keep hammering out the dents between now and May 15th. If it seems usable in real life, I'll write more.

