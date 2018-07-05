# Zlack: a minimalist command-line Slack client

- Created by Andrew Plotkin <erkyrath@eblong.com>
- [MIT license][license]

[license]: ./LICENSE
[slackpost]: http://blog.zarfhome.com/2018/03/open-letter-slack-should-not.html

As you may know, Slack turned off their IRC and XMPP gateways on May 15th. (I [wrote an objection to this][slackpost], but it didn't help.) This project is a replacement for those gateways.

This is *not* a full-on Slack client! It's a replacement for the way I used the XMPP gateway: a little window in the corner of my screen where I can keep an eye on a few important Slack groups and type quick replies. Plain text only. It doesn't try to handle every fancy new feature that Slack rolls out. If you want to do something fancy, you pull up the official Slack client or the web page.

## Installation

This tool is written in Python3. You'll need a recent version of that. You'll also need some packages:

> `pip3 install slackclient aiohttp aiodns websockets`

(This is written to [prompt-toolkit 1.0.15][prompt-toolkit]. This toolkit is undergoing active development and future major revisions may not work the same, so keep an eye out for changes.)

(Note that I am not using the official [Python slackclient][slackclient] library. It's crufty and doesn't support async code, so I wrote my own. Sorry! Feel free to borrow this one, folks...)

[slackclient]: https://github.com/slackapi/python-slackclient
[prompt-toolkit]: https://github.com/jonathanslenders/python-prompt-toolkit

You'll also have to create your own Slack app client ID. This repository doesn't include any such ID, because Slack doesn't want those IDs to be publicized.

Visit [Slack's developer page][slackapp] and create a new app. Then, under "Permissions", add `http://localhost:8090/` as a redirect URL.

[slackapp]: https://api.slack.com/apps

Once you've done this, set the `ZLACK_CLIENT_ID` and `ZLACK_CLIENT_SECRET` environment variables to the values shown on your "App Credentials" page. 

(The developer page suggests that you add "features or permissions scopes", but you don't have to. You're not going to be submitting this to their App Directory.)

## Authentication

Run `zlack.py`, and then type `/auth` to authenticate. (The environment variables must be set for this command to work.)

> `python3 zlack.py`

If you don't know what an environment variable is, you can type everything out on the command line:

> `python3 zlack.py --id CLIENT_ID --secret CLIENT_SECRET`

...where *CLIENT_ID* and *CLIENT_SECRET* are the long hex strings you got off the developer page.

When you type `/auth`, the script will display a Slack URL to visit. It also starts listening on localhost port 8090. Go to the Slack URL in your web browser, authorize the client, and then you will be redirected back to the localhost port. Once this succeeds, your authentication token will be written into `~/.zlack-tokens`.

Since your tokens are saved, you will not need to authenticate again on the same machine. Unless you delete the `~/.zlack-tokens` file.

## Running the client

Once you're authenticated, you can Slack away! 

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

If you type a semicolon on a line by itself, it will switch your default channel to match the last message you *received*. (If you're reading several conversations at once, look at the prompt to make sure you got what you thought!)

There are a handful of special commands, which start with a slash.

- */help* -- this list
- */auth* -- request authentication to a Slack team
- */connect [team]* -- connect (or reconnect) to a team
- */disconnect [team]* -- disconnect from a team
- */teams* -- list all teams you are authorized with
- */users [team]* -- list all users in the current team or a named team
- */channels [team]* -- list all channels in the current team or a named team
- */reload [team]* -- reload users and channels for a team
- */recap \[channel] \[interval]* -- recap an amount of time (default five minutes) on the current channel or a named channel
- */alias [team] alias,alias,...* -- set the aliases for a team
- */debug [bool]* -- set stream debugging on/off or toggle


## Work in progress

The client is usable, but -- of course -- not complete in any sense.

The client doesn't handle status changes such as new users or new channels. In particular, it won't recognize the new IM channel that is created the first time you talk to a given user. Use the */reload* command to fetch an up-to-date user and channel information.

Features the client does not currently handle which I might someday add:

- multi-person IM chat
- threaded messages
- the you-are-typing signal
- telling Slack what messages you've seen
- alerts and notifications
- muting some channels just in this client
- muting Slackbot entirely
- deactivating/muting an entire team
- parallel support for [Mattermost][]?

[Mattermost]: https://about.mattermost.com/

(Not listed: all the features which I don't intend to add. I'm not even trying to keep up with Slack here.)
