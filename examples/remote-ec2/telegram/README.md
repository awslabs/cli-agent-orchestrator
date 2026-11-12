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

## 2. Put the token on the box

Over SSH, in your own shell (not in an agent pane):

```bash
install -d -m 0700 ~/.config/cao-box
( umask 077; read -rs -p 'Bot token: ' t; echo; printf 'TELEGRAM_BOT_TOKEN=%s\n' "$t" > ~/.config/cao-box/telegram.env )
stat -c '%a %U %n' ~/.config/cao-box/telegram.env   # expect: 600 <box-user> ...
```

`read -s` keeps the token off the screen and out of your shell history. Only
the relay unit reads this file. It never goes into `/etc/cao-box/box.env`, the
herdr unit or the cao-server unit, because every agent pane would see it.

## 3. Choose and install a relay

The sample doesn't ship a relay. Any relay that bridges a Telegram bot to
herdr panes works if it:

- uses **outbound** long polling (no webhook, no open port, no tunnel);
- reads the token from the environment (`TELEGRAM_BOT_TOKEN`) or from a file;
- takes the herdr session name (`CAO_BOX_SESSION` from `/etc/cao-box/box.env`).

One choice is [herdr-remote](https://github.com/dcolinmorgan/herdr-remote)
(AGPL-3.0-or-later). Follow its own install steps as the box user, and turn
off any tunnel or web UI it offers if you don't need them. Read its licence
before you change or redistribute it. This sample only links to it.

## 4. Run it under systemd

Copy `../systemd/cao-telegram-relay.service.example` to
`/etc/systemd/system/cao-telegram-relay.service`. Replace `@BOX_USER@`,
`@BOX_HOME@` and `RELAY_COMMAND` (the relay's start command, absolute path
first). Then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cao-telegram-relay.service
journalctl -u cao-telegram-relay -n 50   # it should say it is polling
```

If your relay expects other names for the token or the session, map them in
the unit with `Environment=` or in the relay's own config file. Don't rename
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
