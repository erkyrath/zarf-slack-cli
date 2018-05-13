# Zlack: a minimalist command-line Slack client

- Created by Andrew Plotkin <erkyrath@eblong.com>
- [MIT license][license]

[license]: ./LICENSE
[slackpost]: http://blog.zarfhome.com/2018/03/open-letter-slack-should-not.html

As you probably know, Slack is turning off their IRC and XMPP gateways on May 15th. (I have [written an objection to this][slackpost], but it didn't help.) This project is a replacement for those gateways.

This is *not* a full-on Slack client! It's a replacement for the way I used the XMPP gateway: a little window in the corner of my screen where I can keep an eye on a few important Slack groups and type quick replies. Plain text only. It doesn't try to handle every fancy new feature that Slack rolls out. If you want to do something fancy, you pull up the official Slack client or the web page.

## Installation

This tool is written in Python3. You'll need a recent version of that. You'll also need two packages:

> `pip3 install slackclient prompt-toolkit`

(This is written to [slackclient 1.2.1][slackclient] and [prompt-toolkit 1.0.15][prompt-toolkit]. Both toolkits are undergoing active development and future major revisions may not work the same, so keep an eye out for changes.)

[slackclient]: https://github.com/slackapi/python-slackclient
[prompt-toolkit]: https://github.com/jonathanslenders/python-prompt-toolkit

You'll also have to create your own Slack app client ID. This repository doesn't include any such ID, because Slack doesn't want those IDs to be publicized.

Visit [Slack's developer page][slackapp] and create a new app. Then, under "Permissions", add `http://localhost:8090/` as a redirect URL.

[slackapp]: https://api.slack.com/apps

Once you've done this, set the `ZLACK_CLIENT_ID` and `ZLACK_CLIENT_SECRET` environment variables to the values shown on your "App Credentials" page. 

(The developer page suggests that you add "features or permissions scopes", but you don't have to. You're not going to be submitting this to their App Directory.)

## Authentication

Run `zlack-auth.py` to authenticate. (The environment variables must be set.)

> `python3 zlack-auth.py login`

If you don't know what an environment variable is, you can type everything out on the command line:

> `python3 zlack-auth.py --id CLIENT_ID --secret CLIENT_SECRET login`

...where *CLIENT_ID* and *CLIENT_SECRET* are the long hex strings you got off the developer page.

The script will displays a Slack URL to visit. It also starts listening on localhost port 8090. Go to the Slack URL in your web browser, authorize the client, and then you will be redirected back to the localhost port. Once this succeeds, your authentication token will be written into `~/.zlack-tokens`.

## Setting aliases

Slack team names can be long, so it's good to have some nicknames for them. Type something like

> `python3 zlack-auth.py alias Zarfhome zh`

Then "zh" will be treated as a synonym for the "Zarfhome" team.

You can set several aliases for a team. The first will be used when printing messages.

## Running the client

Now you can run `zlack-client.py`. (This does not need the environment variables.) 

> `python3 zlack-client.py`

At first you have no channel set. So to send a message, type

> *\#team/channel* Message text to send.

(Again, you can use team aliases here.)

Once you send a message on a channel, it becomes the default channel. You don't need to use the channel prefix for subsequent messages. The current channel is always displayed as the input prompt, so you know where you're typing to.

The channel prefix can be any of:

- *\#team/channel* -- a channel of a particular team
- *\#channel* -- a channel of the current team
- *\#team/@user* -- the private message channel to a user on a particular team
- *\#@user* -- the private message channel to a user on the current team

(Multi-person message channels are not yet supported.)

There are a handful of special commands, which start with a slash.

- */help* -- this list
- */teams* -- list all teams you are authorized with
- */connect [team]* -- connect (or reconnect) to a team
- */disconnect [team]* -- disconnect from a team
- */reload [team]* -- reload users and channels for a team
- */channels [team]* -- list all channels in the current team or a named team
- */users [team]* -- list all users in the current team or a named team
- */recap \[channel] \[interval]* -- recap an amount of time (default five minutes) on the current channel or a named channel
- */debug [bool]* -- set stream debugging on/off or toggle


## Work in progress

I'll keep hammering out the dents between now and May 15th. If it seems usable in real life, I'll declare it to be 1.0.

The client doesn't handle status changes such as new users or new channels. In particular, it won't recognize the new IM channel that is created the first time you talk to a given user. Use the */reload* command to fetch an up-to-date user and channel information.

Features this does not currently handle which I might someday add:

- multi-person IM chat
- threaded messages
- the you-are-typing signal
- telling Slack what messages you've seen
- alerts and notifications
- muting some channels just in this client
- deactivating/muting an entire team

(Not listed: all the features which I don't intend to add. I'm not even trying to keep up with Slack here.)
