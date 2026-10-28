# Run plane finishes

`check.sh` posts a run with nothing pending to `POST /agui/v1/run` and passes only if the body carries
`RUN_FINISHED` and closes within 5 s, which is what a stock `@ag-ui/client` needs to complete a run.
Start a server with `CAO_AGUI_ENABLED=1 cao-server`, then run `CAO_API_PORT=9889 ./check.sh`.
On CAO 2.5.4 it prints `FAIL: body did not end within 5s`; with the run-plane fix it prints `PASS`.
It talks to 127.0.0.1 only and needs bash and curl.
