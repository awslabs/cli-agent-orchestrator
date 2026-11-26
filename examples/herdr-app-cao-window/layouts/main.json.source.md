# Where `main.json` came from

`main.json` is a copy of Herdr's shipped `main` layout, taken from an installed Herdr
app 0.1.16 (the version the `main` layout was read from). It is a stand-in for that
version; the installer prefers the live layout of the app you run (below).

The app bundle holds no layout file: Herdr builds `main` into its binary and writes it
to `<home>/layouts/main.json` when it starts. `install.sh` therefore prefers the live
`layouts/main.json` of the main app home and uses this copy only when there is none,
and says so ("may lag").
