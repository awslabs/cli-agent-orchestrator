# Ephemeral-agent band names (1980-1990)

**Issue:** [#801](https://github.com/awslabs/cli-agent-orchestrator/issues/801)
**Related:** [design.md](design.md)

This list supplies the `<Band>` segment of the `<Band>-<purpose>-<hex4>` naming
format defined in ADR-8 of [design.md](design.md). It replaces the earlier neutral-wordlist
decision recorded in section 9 (Q6).

## Selection rules

1. **Era and fame.**
   - It must be a rock **band**, not a solo artist.
   - Any rock subgenre counts: hard rock, heavy metal, glam, new wave,
     post-punk, alternative or college rock.
   - The band must have a widely known album or hit single released
     **1980-1990** inclusive.
   - A band formed earlier qualifies if its best-known work falls in that
     window (for example AC/DC, *Back in Black*, 1980).
   - A band is included only if the release and its year are confident;
     anything unsure was left out.
2. **Normalization.**
   - Drop a leading "The".
   - Remove spaces, punctuation and diacritics, and CamelCase the words
     (`AC/DC` becomes `ACDC`; `Guns N' Roses` becomes `GunsNRoses`;
     `R.E.M.` becomes `REM`).
   - Keep acronyms in capitals (`INXS` stays `INXS`).
   - Spell out a leading number (`10,000 Maniacs` becomes
     `TenThousandManiacs`).
   - A band's own widely used short form may be used when the full name
     exceeds 24 characters (Orchestral Manoeuvres in the Dark becomes
     `OMD`).
   - The result must match `^[A-Z][A-Za-z0-9]{1,23}$`.
3. **Uniqueness.**
   - Normalized names are unique case-insensitively.
   - No normalized name equals, case-insensitively, a built-in CAO profile
     name. Checked read-only against:
     - `git ls-tree --name-only origin/main src/cli_agent_orchestrator/agent_store/` (run against a local clone of the CAO repo)
4. **Log-safety exclusions.**
   - Excludes names that read as violence, weapons, disease, drugs, sexual
     content, or security/malware terms (for example Anthrax, Slayer,
     Megadeth, Poison, Venom), because these names appear in logs, dashboards
     and alerts, where they can alarm people or trip security tooling.
   - Each exclusion is recorded in the Excluded table below with the rule it
     broke.

## Included (94 names)

| # | Name (normalized) | Band as known | Defining release (year) |
|---|-------------------|---------------|-------------------------|
| 1 | ACDC | AC/DC | Back in Black (1980) |
| 2 | GunsNRoses | Guns N' Roses | Appetite for Destruction (1987) |
| 3 | BonJovi | Bon Jovi | Slippery When Wet (1986) |
| 4 | DefLeppard | Def Leppard | Hysteria (1987) |
| 5 | IronMaiden | Iron Maiden | The Number of the Beast (1982) |
| 6 | JudasPriest | Judas Priest | Screaming for Vengeance (1982) |
| 7 | MotleyCrue | Mötley Crüe | Shout at the Devil (1983) |
| 8 | VanHalen | Van Halen | 1984 (1984) |
| 9 | Metallica | Metallica | Master of Puppets (1986) |
| 10 | Aerosmith | Aerosmith | Permanent Vacation (1987) |
| 11 | Whitesnake | Whitesnake | Whitesnake (1987) |
| 12 | Europe | Europe | The Final Countdown (1986) |
| 13 | Scorpions | Scorpions | Love at First Sting (1984) |
| 14 | TwistedSister | Twisted Sister | Stay Hungry (1984) |
| 15 | Ratt | Ratt | Out of the Cellar (1984) |
| 16 | Dokken | Dokken | Tooth and Nail (1984) |
| 17 | QuietRiot | Quiet Riot | Metal Health (1983) |
| 18 | Cinderella | Cinderella | Night Songs (1986) |
| 19 | Foreigner | Foreigner | 4 (1981) |
| 20 | Journey | Journey | Escape (1981) |
| 21 | Styx | Styx | Paradise Theatre (1981) |
| 22 | Toto | Toto | Toto IV (1982) |
| 23 | Rush | Rush | Moving Pictures (1981) |
| 24 | Queen | Queen | The Game (1980) |
| 25 | Police | The Police | Synchronicity (1983) |
| 26 | U2 | U2 | The Joshua Tree (1987) |
| 27 | REM | R.E.M. | Document (1987) |
| 28 | TalkingHeads | Talking Heads | Remain in Light (1980) |
| 29 | Pretenders | The Pretenders | Learning to Crawl (1984) |
| 30 | Cure | The Cure | Disintegration (1989) |
| 31 | Smiths | The Smiths | The Queen Is Dead (1986) |
| 32 | Cars | The Cars | Heartbeat City (1984) |
| 33 | Blondie | Blondie | Autoamerican (1980) |
| 34 | Devo | Devo | Freedom of Choice (1980) |
| 35 | DuranDuran | Duran Duran | Rio (1982) |
| 36 | DepecheMode | Depeche Mode | Violator (1990) |
| 37 | NewOrder | New Order | Power, Corruption & Lies (1983) |
| 38 | Eurythmics | Eurythmics | Sweet Dreams Are Made of This (1983) |
| 39 | SimpleMinds | Simple Minds | Once Upon a Time (1985) |
| 40 | TearsForFears | Tears for Fears | Songs from the Big Chair (1985) |
| 41 | Yazoo | Yazoo | Upstairs at Eric's (1982) |
| 42 | HumanLeague | The Human League | Dare (1981) |
| 43 | GoGos | The Go-Go's | Beauty and the Beat (1981) |
| 44 | Bangles | The Bangles | Different Light (1986) |
| 45 | Heart | Heart | Heart (1985) |
| 46 | Genesis | Genesis | Invisible Touch (1986) |
| 47 | Yes | Yes | 90125 (1983) |
| 48 | Asia | Asia | Asia (1982) |
| 49 | CheapTrick | Cheap Trick | Lap of Luxury (1988) |
| 50 | Dio | Dio | Holy Diver (1983) |
| 51 | SkidRow | Skid Row | Skid Row (1989) |
| 52 | Warrant | Warrant | Dirty Rotten Filthy Stinking Rich (1989) |
| 53 | Winger | Winger | Winger (1988) |
| 54 | Tesla | Tesla | The Great Radio Controversy (1989) |
| 55 | Kix | Kix | Blow My Fuse (1988) |
| 56 | LosLobos | Los Lobos | How Will the Wolf Survive? (1984) |
| 57 | StrayCats | Stray Cats | Built for Speed (1982) |
| 58 | ThompsonTwins | Thompson Twins | Into the Gap (1984) |
| 59 | ABC | ABC | The Lexicon of Love (1982) |
| 60 | Squeeze | Squeeze | East Side Story (1981) |
| 61 | XTC | XTC | English Settlement (1982) |
| 62 | Housemartins | The Housemartins | London 0 Hull 4 (1986) |
| 63 | Smithereens | The Smithereens | Especially for You (1986) |
| 64 | Replacements | The Replacements | Let It Be (1984) |
| 65 | HuskerDu | Hüsker Dü | Zen Arcade (1984) |
| 66 | Pixies | Pixies | Doolittle (1989) |
| 67 | SonicYouth | Sonic Youth | Daydream Nation (1988) |
| 68 | DinosaurJr | Dinosaur Jr. | Bug (1988) |
| 69 | INXS | INXS | Kick (1987) |
| 70 | CrowdedHouse | Crowded House | Crowded House (1986) |
| 71 | SplitEnz | Split Enz | True Colours (1980) |
| 72 | MenAtWork | Men at Work | Business as Usual (1981) |
| 73 | Aha | a-ha | Hunting High and Low (1985) |
| 74 | CultureClub | Culture Club | Colour by Numbers (1983) |
| 75 | FrankieGoesToHollywood | Frankie Goes to Hollywood | Welcome to the Pleasuredome (1984) |
| 76 | SpandauBallet | Spandau Ballet | True (1983) |
| 77 | PsychedelicFurs | The Psychedelic Furs | Talk Talk Talk (1981) |
| 78 | EchoAndTheBunnymen | Echo & the Bunnymen | Ocean Rain (1984) |
| 79 | SiouxsieAndTheBanshees | Siouxsie and the Banshees | Kaleidoscope (1980) |
| 80 | Alarm | The Alarm | Declaration (1984) |
| 81 | TenThousandManiacs | 10,000 Maniacs | In My Tribe (1987) |
| 82 | TalkTalk | Talk Talk | It's My Life (1984) |
| 83 | Erasure | Erasure | The Innocents (1988) |
| 84 | BigCountry | Big Country | The Crossing (1983) |
| 85 | Berlin | Berlin | Pleasure Victim (1982) |
| 86 | AFlockOfSeagulls | A Flock of Seagulls | A Flock of Seagulls (1982) |
| 87 | Stranglers | The Stranglers | La Folie (1981) |
| 88 | Survivor | Survivor | Eye of the Tiger (1982) |
| 89 | NightRanger | Night Ranger | Midnight Madness (1983) |
| 90 | Loverboy | Loverboy | Get Lucky (1981) |
| 91 | GreatWhite | Great White | Once Bitten (1987) |
| 92 | Autograph | Autograph | Sign in Please (1984) |
| 93 | FireHouse | FireHouse | FireHouse (1990) |
| 94 | OMD | Orchestral Manoeuvres in the Dark | Architecture & Morality (1981) |

## Excluded

| Candidate | Rule broken / reason |
|---|---|
| Anthrax | Log-safety: disease/bioweapon term |
| Slayer | Log-safety: violence term |
| Megadeth | Log-safety: violence/weapons term (mass death) |
| Poison | Log-safety: drugs/toxin term |
| Venom | Log-safety: drugs/toxin term |
| W.A.S.P. | Log-safety: could read as a slur or insect-attack term in logs; the stylized punctuation also does not normalize cleanly |
| Dead Kennedys | Log-safety: "Dead" plus a political-assassination reference reads as violence/alarming in logs |
| Black Flag | Log-safety: carries anarchist/violent-symbolism connotations |
| Faster Pussycat | Log-safety: sexual-content connotation in the name itself |
| Gang of Four | Log-safety: politically loaded phrase associated with violent purges; alarming in logs/dashboards |
| Suicidal Tendencies | Log-safety: self-harm term |
| Butthole Surfers | Log-safety: sexual/vulgar content |
| Soft Cell | Log-safety: defining album title ("Non-Stop Erotic Cabaret") is sexual-content-adjacent; excluded out of caution even though the band name itself is neutral |
| L.A. Guns | Log-safety (maintainer, 2026-09-30): the normalized form reads as weapons in logs/dashboards |
| Joy Division | Log-safety (maintainer, 2026-09-30): the name refers to a Nazi-era forced-prostitution unit |
| Pat Benatar | Not a rock band (rule 1): solo artist |
| Run-D.M.C. | Not a rock band (rule 1): hip hop |
| Beastie Boys | Not a rock band (rule 1): hip hop |
| UB40 | Not a rock band (rule 1): reggae |
| Wham! | Not a rock band (rule 1): pop |
| Bananarama | Not a rock band (rule 1): pop |
| Kajagoogoo | Not a rock band (rule 1): pop |
| Level 42 | Not a rock band (rule 1): jazz-funk/pop |
| Led Zeppelin | Era/fame (rule 1): the listed album (*In Through the Out Door*) is from 1979, and the band's best-known work is 1970s |
| The Who | Era/fame (rule 1): best-known work (*Who's Next*, *Tommy*) is 1960s-1970s |
| The Rolling Stones | Era/fame (rule 1): best-known work spans the 1960s-1970s |
| Black Sabbath | Era/fame (rule 1): best-known work (with Ozzy Osbourne, 1970-1978) is 1970s |
| Thin Lizzy | Era/fame (rule 1): best-known work (*Jailbreak*, "The Boys Are Back in Town") is 1976 |
| Blue Öyster Cult | Era/fame (rule 1): best-known work ("(Don't Fear) The Reaper") is 1976 |
| Fleetwood Mac | Era/fame (rule 1): best-known work (*Rumours*) is 1977 |
| Boston | Era/fame (rule 1): best-known work (*Boston*, "More Than a Feeling") is 1976 |
| Ambrosia | Era/fame (rule 1): best-known work ("How Much I Feel", "Biggest Part of Me") is 1978-1980, and the band is not confidently famous enough in the 1980-1990 window specifically |
| Chicago | Era/fame (rule 1): best-known work spans the 1970s (*Chicago* I-XI); withdrawn on a consistency pass, since it is the same "best-known work predates the window" pattern as Zeppelin/Sabbath/the Stones |
| Bauhaus | Era/fame (rule 1): the band's most iconic and widely known song, "Bela Lugosi's Dead", is a 1979 single; not confident the 1980-1990 window is where its defining fame sits |
| Cocteau Twins | Era/fame: not confident of a single widely known 1980-1990 hit/album meeting the fame bar with the same confidence as the included list |
| Buzzcocks | Era/fame: defining work (*Singles Going Steady*) is 1979; not confident of a 1980-1990 defining release |
| Wire | Era/fame: defining work (*Pink Flag*, *Chairs Missing*) is 1977-1978; not confident of a 1980-1990 defining release |
| X-Ray Spex | Era/fame: defining work (*Germfree Adolescents*) is 1978; not confident of a 1980-1990 defining release |
| UK (band) | Era/fame: defining work (*Danger Money*) is 1979; not confident of a 1980-1990 release, and "UK" alone is too short/ambiguous to normalize meaningfully |
| Tubeway Army / Gary Numan | Era/fame: defining work (*Replicas*) is 1979, and Gary Numan is primarily a solo artist, not a band |
| The Passions | Era/fame: not confident the listed release is widely known enough to meet the fame bar |

## Validation

Commands run against the included table's normalized-name column (94 rows),
from a scratch file under a single `mktemp -d` directory (removed after use).

**1. Every normalized name matches the regex** (count of non-matching names,
must be 0):

```
$ cut -f1 candidates.tsv | grep -Evc '^[A-Z][A-Za-z0-9]{1,23}$'
0
```

**2. No case-insensitive duplicates** (`sort -f | uniq -di` must print
nothing):

```
$ cut -f1 candidates.tsv | sort -f | uniq -di
(no output)
```

**3. No clash with built-in profile names** (checked case-insensitively against `src/cli_agent_orchestrator/agent_store/`; no output means no clash):

```
$ git ls-tree --name-only origin/main src/cli_agent_orchestrator/agent_store/
# code_supervisor.md developer.md memory_manager.md retrospector.md
# reviewer.md workflow_scout.md

$ cut -f1 candidates.tsv | tr '[:upper:]' '[:lower:]' | sort > band_lc.txt
$ for p in <every profile name above>; do grep -x "$p" band_lc.txt; done
(no output — no clashes)
```

**4. Final count of included names:**

```
$ wc -l < candidates.tsv
94
```

All four checks pass: 0 regex failures, 0 duplicates, 0 profile-name clashes,
94 included names.
