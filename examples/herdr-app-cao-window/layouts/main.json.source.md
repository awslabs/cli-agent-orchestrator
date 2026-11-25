# Where `main.json` came from

`main.json` is a copy of Herdr's shipped `main` layout, made from
`examples/herdr-app-cao-tab/layouts/main-cao.json` (commit `f2809d7b`) with its
`cao-side` block removed.

Herdr app version: not recorded (cloned from the shipped `main` for the cao-tab
example). Spike 1.9 read 0.1.16 from the installed app; its `main` is identical to
the cao-tab `main-cao.json` minus `cao-side`, so this copy stands for 0.1.16.

The app bundle holds no layout file: Herdr builds `main` into its binary and writes it
to `<home>/layouts/main.json` when it starts. `install.sh` therefore prefers the live
`layouts/main.json` of the main app home and uses this copy only when there is none,
and says so ("may lag").
