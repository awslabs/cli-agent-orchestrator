# A second Telegram bot for the box

Give the box its own bot. Your laptop's bot keeps working while you move
things over, and the two never compete for the same updates. Do one step at a
time, and check each one before you move on.

## 1. Create the bot (on your phone or Telegram desktop)

1. Open a chat with **@BotFather** and send `/newbot`.
2. Give it a display name, for example `CAO box`.
3. Give it a username. It must end in `bot`, for example `<you>_cao_box_bot`.
4. BotFather replies with a **token**. Treat it like a password. Don't paste it
   into a chat, a ticket or a pane that an agent can read.

If the token leaks, send `/token` to BotFather and pick the bot. That gives
you a new token and the old one stops working.

## 2. Make the relay its own user

The agents run as the box user, so anything that user owns, agents can read.
The relay therefore runs as a separate system user, `cao-relay`, and only
that user can read the token. In your admin shell (the Session Manager
shell, not as the box user and not in an agent pane):

```bash
sudo useradd --system --home-dir /var/lib/cao-relay --shell /usr/sbin/nologin cao-relay
```

## 3. Put the token on the box

```bash
sudo install -d -m 0755 /etc/cao-box
sudo install -o cao-relay -g cao-relay -m 0600 /dev/null /etc/cao-box/telegram.env
read -rs -p 'Bot token: ' t; echo
printf 'TELEGRAM_BOT_TOKEN=%s\n' "$t" | sudo tee /etc/cao-box/telegram.env >/dev/null
unset t
sudo stat -c '%a %U %n' /etc/cao-box/telegram.env   # expect: 600 cao-relay ...
```

`read -s` keeps the token off the screen and out of your shell history, and
`tee` keeps the file's owner and mode. Only the relay unit reads this file.
It never goes into `/etc/cao-box/box.env`, the herdr unit or the cao-server
unit, because every agent pane would see it.

## 4. Choose, install and run a relay

The sample doesn't ship a relay. Any relay that bridges a Telegram bot to
herdr panes works if it:

- uses **outbound** long polling (no webhook, no open port, no tunnel);
- reads the token from the environment (`TELEGRAM_BOT_TOKEN`) or from a file;
- takes the herdr session name (`CAO_BOX_SESSION` from `/etc/cao-box/box.env`).

One choice is [herdr-remote](https://github.com/dcolinmorgan/herdr-remote)
(AGPL-3.0-or-later). Install it where `cao-relay` can run it (for example
under `/opt`), following its own steps, and turn off any tunnel or web UI it
offers if you don't need them. Read its licence before you change or
redistribute it. This sample only links to it.

**The relay needs the herdr socket.** It lives under the box user's
`~/.config/herdr/sessions/<session>/`, which `cao-relay` can't reach by
default. One way is ACLs: let `cao-relay` traverse the folders and use the
socket, with a default ACL on the session folder so the socket herdr creates
at each start inherits it. Anything that can drive the panes can do what an
agent can do, so give the relay exactly that and nothing more. Check this in
phase 2.

Copy `../systemd/cao-telegram-relay.service.example` to
`/etc/systemd/system/cao-telegram-relay.service`. Replace `@BOX_HOME@` and
`RELAY_COMMAND` (the relay's start command, absolute path first). Then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cao-telegram-relay.service
journalctl -u cao-telegram-relay -n 50   # it should say it is polling
```

If your relay expects other names for the token or the session, map them in
the unit with `Environment=` or in the relay's own config file. Don't move
the token file or loosen its mode.

## 5. Say hello

In Telegram, open your new bot and send `/start`. The relay should answer and
list the box's agents. Then send a short prompt to one agent and check that
the reply comes back. That is the Telegram acceptance check in the README.

## Turning it off

```bash
sudo systemctl disable --now cao-telegram-relay.service
```

To retire the bot completely, send `/deletebot` to BotFather.
