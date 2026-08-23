# Radio Stack Roadmap

A 90 day plan for the personal radio stack in this repo. It is scoped to one
operator running Linux and Windows, streaming to Icecast on port 8000, with a
CLI first workflow and a browser console on top.

The plan follows the researcher report at `~/research/radio-automation.md`,
which ranked the gaps against real broadcast automation tools. The top ranked
gap, a clockwheel / hour template, is already being built by a developer and is
marked In Progress below.

Test baseline today: 232 tests (63 broadcast, 108 engine, 61 studio). The
clock feature will add more.

## Phase 1: Now (weeks 1 to 2)

Finish and verify the clock template, add sweepers and jingles to the library
scan, and confirm the radio status path on Windows.

| Item | Why | Deliverable | Acceptance test | Owner |
|------|-----|-------------|-----------------|-------|
| Clockwheel / hour template | This is the single biggest quality gap between the stack and a real station sound, and it is already underway. | A JSON clock template plus interleaving logic in `broadcast/playlistgen.py` that places music, sweepers, and IDs at fixed minute positions. | A generated hour places a legal ID at :00 and a sweeper at a fixed junction, and the new tests pass alongside the existing 63 broadcast tests. | implementation |
| Sweeper / jingle folder support in the library scan | Imaging files need to be first class citizens in the library, not invisible to the scanner. | `scan_folder` and `trackcheck` recognise a sweepers or jingles folder and tag those files as a non music category. | A folder of short jingles scans in and shows up with a sweeper category instead of being treated as music. | implementation |
| Radio status on Windows validation | The engine went cross platform recently and the status path is the least exercised part on Windows. | `radio status` reports stream state correctly under the Windows install, wired into `windows/validate-windows.ps1`. | Running the Windows validator after install reports a live station stream without manual fixes. | owner |

First deliverable of week 1: the clock template lands in `broadcast/playlistgen.py`
with a passing test that a generated hour places a legal ID at :00 and a
sweeper at a fixed junction.

## Phase 2: Next (weeks 3 to 6)

Build the imaging library, ship voice tracking v1, and surface the clock wheel
in the studio console.

| Item | Why | Deliverable | Acceptance test | Owner |
|------|-----|-------------|-----------------|-------|
| Imaging library in trackcheck | Sweepers, IDs, and promos need their own tagged category so the clock can pull from them deliberately. | A tagged imaging category in `trackcheck` that groups sweepers, IDs, and promos separately from music. | A scan reports imaging files under their own category and the clock can reference that category by name. | implementation |
| Voice tracking v1 | Pre recorded liners between songs make the station sound live without being live, the second ranked gap. | Pre recorded liners injected at clock positions, with a short research note on audio format and how RadioDJ models it. | A liner file plays at a scheduled clock position between two songs in a generated hour. | implementation |
| Clock wheel visual in studio | The operator needs to see the hour at a glance, not read a JSON file. | A read only hour view in the studio console showing the :00, :14, and :30 markers with the tracks and imaging at each position. | The studio page renders the current hour wheel with the three markers and the correct items at each position. | ui-designer |

## Phase 3: Later (weeks 7 to 12)

Add cue point analysis, multi channel scheduling, and polish the mobile listen
page.

| Item | Why | Deliverable | Acceptance test | Owner |
|------|-----|-------------|-----------------|-------|
| Cue point analysis | Blind crossfades cut off intros and outros; cue points let Liquidsoap fade at the right moment. | Intro and outro detection via librosa or ffmpeg silencedetect, with a short note on which fits the stack, stored per track. | A track with a long intro reports an intro cue point and the crossfade starts after it. | implementation |
| Multi channel scheduling | Two streams need two schedules without duplicating the whole config. | A second stream schedule that reuses the existing rotation and clock logic. | Two streams run from one library with independent schedules and no shared state collision. | implementation |
| Mobile listen page polish | The listen page already exists via the Icecast URL; it just needs to feel finished. | A cleaned up listen page that works on a phone and shows now playing. | The page loads on a phone, shows the current track, and plays the stream. | ui-designer |

## Non goals

These are deliberate exclusions to keep the scope tight.

| Item | Why it is out of scope |
|------|------------------------|
| Live assist on Windows | PulseAudio is Linux only, so live assist stays Linux only and Windows keeps the station stream. |
| Full Rivendell or AzuraCast replacement | This is a single person stack with a CLI first workflow; a full broadcast suite is overkill. |
| Ad insertion automation | Ads are not part of a personal station and automating them adds scheduling complexity for no benefit. |
| The call sheet for radio | The carscout PDF pattern applied to station promos is a nice idea but it is scope creep and is skipped. |
