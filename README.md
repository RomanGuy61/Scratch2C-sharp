# Scratch → Unity C# Converter

A Flet-based Linux GUI app: pick a `.sb3` Scratch project, pick a sprite,
get a best-effort Unity `MonoBehaviour` translation of its scripts.

## Running

```
chmod +x ScratchToUnity.x86_64
./ScratchToUnity.x86_64
```

**Linux dependency:** the file picker dialogs use [Zenity](https://help.gnome.org/users/zenity/stable/).
If it's not already on your system: on Bazzite, `rpm-ostree install zenity`
(needs a reboot); on Debian/Ubuntu, `sudo apt-get install zenity`.

First launch needs internet access once — Flet downloads a small native
UI runtime the first time an app runs on a given machine (standard Flet
desktop-app behavior, not specific to this tool). After that it's offline.

If you get `CERTIFICATE_VERIFY_FAILED` on that first launch: this build
bundles its own CA cert bundle (via `certifi`) and points OpenSSL at it
through `runtime_hook_certs.py`, since PyInstaller binaries can otherwise
look for system certs in the wrong place on some distros. If you rebuild
from source, keep `--collect-all certifi` and `--runtime-hook
runtime_hook_certs.py` in `build.sh` or the same error can come back.

## 2D vs 3D output

Add a Scratch variable named **"Project Type"** (on the Stage or on the
sprite) set to the text `Vector2` or `Vector3`. The converter scans for it:

- `Vector2` (or not set) → 2D output: Scratch X/Y maps to Unity X/Y,
  `SpriteRenderer`-based helpers (sorting order, `Collider2D` touching
  checks, etc).
- `Vector3` → 3D output: Scratch X/Y maps to Unity X/Z (a ground plane),
  Unity Y is left as height for you to use, and helpers switch to
  `Collider`/`Renderer` (non-2D) equivalents, plane-raycast mouse
  picking, etc.

This variable itself isn't emitted as a C# field — it's config for the
converter, not a real Scratch variable your scripts use. The generated
class always inherits from the same `ScratchSpriteBase`, but which
`ScratchSpriteBase.cs` gets written (2D vs 3D flavor) depends on this
setting, so make sure every sprite you convert from the same project
agrees on it — otherwise the last one you save will overwrite
`ScratchSpriteBase.cs` with the other flavor.

## Using it

1. **Select .sb3 project** → pick your Scratch project file.
2. Pick a sprite from the dropdown (Stage is excluded — only sprites).
3. **Convert to C#** → generates a `<SpriteName>.cs` MonoBehaviour, and
   shows a coverage line ("N/M generated lines converted cleanly") so
   you know at a glance how much manual follow-up to expect.
4. **Save .cs + assets** → writes `<SpriteName>.cs` and a shared
   `ScratchSpriteBase.cs` into a folder you choose, and extracts that
   sprite's actual costume images and sound files out of the .sb3 into
   `Assets/<SpriteName>/` alongside them (original filenames — a comment
   block atop the .cs maps each Scratch costume/sound name to its file
   so `SetCostume`/`PlaySound` TODOs are easy to wire up).
5. **Convert All Sprites…** → does steps 3–4 for every sprite in the
   project in one pass, into one folder, with one shared
   `ScratchSpriteBase.cs` (3D wins if sprites disagree on Project Type),
   and a per-sprite log of what got written and how clean each came out.

## What the conversion covers

Motion, Looks, Sound, Events, Control, Sensing, Operators, Variables/Lists,
and custom blocks ("My Blocks") are translated for the ~90 opcodes people
actually use day to day: movement, costumes, say/think, broadcasts,
loops/if/wait/clone, touching/key/mouse checks, math, and variable/list
ops. Scratch's "when flag clicked" / "when key pressed" / "when I receive"
hats become Unity coroutines wired up in `Start()`/`Update()`.

Anything not recognized is emitted as a `// TODO: <opcode>` comment
instead of being silently dropped, so you can find and finish those by
hand. `ScratchSpriteBase.cs` has stub methods (say/think bubbles, costume
swapping, graphic effects, touching-edge, microphone loudness, ask-and-
wait UI) that need real implementations wired to your project's UI/camera/
sprites — Scratch and Unity don't have 1:1 equivalents for these, so they're
left as clearly marked TODOs rather than guessed at.

Not implemented: Pen extension, cloud variables, "touching color" pixel
checks, and video/microphone sensing beyond stubs.

## Generate a whole Unity project

**Generate Unity Project…** goes further than the other buttons: point it
at an empty/new folder and it writes out an entire openable Unity
project — not just scripts.

- `Assets/Scripts/` — every sprite's `.cs` plus `ScratchSpriteBase.cs`
- `Assets/Sprites/<Sprite>/`, `Assets/Sounds/<Sprite>/` — extracted costume/sound files
- `Assets/Scripts/Editor/ScratchSceneBuilder.cs` — builds the actual scene
  the first time you open the project in Unity (via `[InitializeOnLoad]`),
  creating one GameObject per sprite with its script attached, wiring up a
  shared `Canvas` for anything marked UI, and adding a Main Camera if no
  sprite claimed the camera role. Re-run it anytime from
  **Tools > Scratch Import > Build Scene Now** (this rebuilds from
  scratch, discarding manual scene edits).
- `Assets/Scripts/Editor/ScratchTexturePostprocessor.cs` — forces costume
  images to import as `Sprite` type so they actually work in
  `SpriteRenderer`/`Image` components.
- `ProjectSettings/ProjectVersion.txt` + `Packages/manifest.json` — the
  minimum Unity needs to recognize and open the folder as a project.
  Targets Unity 2022.3.21f1 LTS; Unity Hub will offer to install that or
  open with whatever you already have — nothing here is version-specific.
- `README_FIRST_OPEN.md` in the generated project — what happened, what
  role each sprite got, and the known rough edges (SVG costumes, UI
  positioning, camera role limitations).

Why an Editor script instead of a hand-written `.unity` scene file: Unity's
scene format is YAML with internal `fileID`/`guid` cross-references that
are easy to get subtly wrong, producing a scene Unity silently refuses to
load correctly. Building it live through the real `UnityEditor` API
sidesteps that entirely, and it doubles as a "regenerate" button whenever
you reconvert.

### The "Object type: ()" custom block

Add a custom block named **"Object type: ()"** with one text/number
parameter, and call it once per sprite (anywhere — its call sites become
no-ops in the generated code) with the value `UI`, `Camera`, or leave it
uncalled for a regular GameObject. This tells the Unity project generator
how to build that sprite in the scene:

- **`GameObject`** (default) → a plain GameObject with the sprite's
  script attached — `SpriteRenderer` + `BoxCollider2D` + `AudioSource` in
  2D projects, or a placeholder `Cube` (swap in your real model) in 3D.
- **`UI`** → a `RectTransform`-based `Image` under a shared `Canvas`
  (created once, shared by every UI sprite), for menus/HUDs/buttons.
- **`Camera`** → the sprite's script is attached directly to a `Camera`
  GameObject instead — handy for a player-follow script, less useful for
  anything that switches costumes.

## Custom "My Blocks" with built-in support

Since Scratch has no native 3D blocks, these custom block shapes (label
text matters, parameter names don't) get inlined as real Unity transform
ops at every call site instead of becoming a stub coroutine call:

- **`move x: () y: () z: ()`** (3 number params) → `transform.position = new Vector3(x, y, z)` (absolute, not relative — adjust to `+=` yourself if you meant "move by")
- **anything with "rotate"/"rotation"** → 3 params: `transform.eulerAngles = new Vector3(rx, ry, rz)`; 1 param: `transform.Rotate(Vector3.up, angle)`
- **anything with "scale"** → 3 params: `transform.localScale = new Vector3(sx, sy, sz)`; 1 param: uniform `transform.localScale = Vector3.one * scale`
- **`Object type: ()`** → not runtime code at all — read once by "Generate Unity Project" to decide whether the sprite becomes a GameObject, a UI element, or a Camera (see below).

Their Scratch-side definitions are skipped entirely (no method is
generated for them) since only the call sites matter here. Everything
else you define as a custom block still gets a normal generated method.

## Rebuilding from source

`build.sh` recreates the venv and the `.x86_64` binary via PyInstaller.
Source files: `main.py` (Flet GUI), `sb3_parser.py` (reads project.json
out of the .sb3 zip), `csharp_converter.py` (block → C# expression/
statement rules), `script_builder.py` (assembles the full class per
sprite + the shared base class).
