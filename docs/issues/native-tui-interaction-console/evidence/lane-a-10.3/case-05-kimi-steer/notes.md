fused v3 steer [text, chord C-s] mid-turn: outcome='refused' reason='pane-busy' detail='the receiver is processing, not idle; a composer-class sequence is readiness-gated and nothing was written'
v2 steer control (text + chord C-s, enter=false): outcome='accepted' chord_sent=True.
post-steer transcript lines naming the steer: ['✨ Please reconsider this path']
DEVIATION (vs a naive reading of §10.3): the fused v3 [text, chord] steer is readiness-gated as a whole (any composer-class event gates the batch) and is refused pane-busy mid-turn with zero bytes.  The deployed mid-turn steer path is the v2 steer control (proven accepted above).
fused v3 steer [text, chord C-s] mid-turn: outcome='refused' reason='pane-busy' detail='the receiver is processing, not idle; a composer-class sequence is readiness-gated and nothing was written'
v2 steer control (text + chord C-s, enter=false): outcome='accepted' chord_sent=True.
post-steer transcript lines naming the steer: ['✨ Please reconsider this path']
DEVIATION (vs a naive reading of §10.3): the fused v3 [text, chord] steer is readiness-gated as a whole (any composer-class event gates the batch) and is refused pane-busy mid-turn with zero bytes.  The deployed mid-turn steer path is the v2 steer control (proven accepted above).
