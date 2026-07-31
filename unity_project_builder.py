"""
Builds a full, openable Unity project folder from a parsed SB3Project:
scripts + assets (via script_builder/sb3_parser) plus the Unity-specific
scaffolding (ProjectSettings, Packages/manifest.json) and an Editor script
that builds the actual scene the first time the project is opened.

Design choice: rather than hand-writing Unity's YAML .unity scene format
(fragile — easy to get a fileID/guid subtly wrong and produce a scene
Unity refuses to load), we ship a small Editor-only C# script that builds
the scene *in Unity* using the real UnityEditor/UnityEngine APIs. It runs
automatically once via [InitializeOnLoad], and is also exposed as
Tools > Scratch Import > Build Scene Now so it can be re-run on demand.
"""
import json
import os

from csharp_converter import detect_object_type, sanitize_ident
from script_builder import generate_csharp, get_base_class_template, build_class_name

UNITY_VERSION = "2022.3.21f1"  # widely-available LTS; Unity Hub will offer to switch/install if it differs from what's on your machine


def _csharp_string(s: str) -> str:
    s = str(s).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def generate_unity_project(project, dest_dir: str, log_prefix: str = ""):
    """Writes a complete Unity project into dest_dir (created if needed).
    Returns a list of human-readable log lines describing what happened."""
    log = []

    assets = os.path.join(dest_dir, "Assets")
    scripts_dir = os.path.join(assets, "Scripts")
    editor_dir = os.path.join(scripts_dir, "Editor")
    sprites_dir = os.path.join(assets, "Sprites")
    sounds_dir = os.path.join(assets, "Sounds")
    scenes_dir = os.path.join(assets, "Scenes")
    project_settings_dir = os.path.join(dest_dir, "ProjectSettings")
    packages_dir = os.path.join(dest_dir, "Packages")

    for d in (scripts_dir, editor_dir, sprites_dir, sounds_dir, scenes_dir, project_settings_dir, packages_dir):
        os.makedirs(d, exist_ok=True)

    sprites = project.non_stage_sprites()
    if not sprites:
        log.append("✗ No sprites found — nothing to generate.")
        return log

    # --- Per-sprite conversion (scripts + assets), same as "Convert All" ---
    sprite_meta = []  # list of dicts describing each sprite for the scene builder
    any_3d = False
    for sprite in sprites:
        try:
            code, is_3d = generate_csharp(sprite, project)
            any_3d = any_3d or is_3d
            class_name = build_class_name(sprite.name)
            with open(os.path.join(scripts_dir, f"{class_name}.cs"), "w") as f:
                f.write(code)

            extracted_costumes = _extract_filtered(project, sprite.costume_assets, os.path.join(sprites_dir, class_name))
            extracted_sounds = _extract_filtered(project, sprite.sound_assets, os.path.join(sounds_dir, class_name))

            object_type = detect_object_type(sprite)
            first_raster_costume = next(
                (a.filename for a in sprite.costume_assets
                 if a.filename.lower().endswith((".png", ".jpg", ".jpeg"))),
                None,
            )
            sprite_meta.append({
                "class_name": class_name,
                "sprite_name": sprite.name,
                "object_type": object_type,
                "costume_rel_path": f"Assets/Sprites/{class_name}/{first_raster_costume}" if first_raster_costume else None,
                "has_svg_only_costume": bool(sprite.costume_assets) and not first_raster_costume,
            })
            log.append(f"✓ {sprite.name} → Scripts/{class_name}.cs "
                        f"({len(extracted_costumes)} costume(s), {len(extracted_sounds)} sound(s))")
        except Exception as ex:
            log.append(f"✗ {sprite.name} FAILED: {ex}")

    # --- Shared base class ---
    with open(os.path.join(scripts_dir, "ScratchSpriteBase.cs"), "w") as f:
        f.write(get_base_class_template(any_3d))
    log.append(f"— wrote Scripts/ScratchSpriteBase.cs ({'3D' if any_3d else '2D'})")

    # --- Editor scaffolding ---
    with open(os.path.join(editor_dir, "ScratchTexturePostprocessor.cs"), "w") as f:
        f.write(_TEXTURE_POSTPROCESSOR_CS)
    with open(os.path.join(editor_dir, "ScratchSceneBuilder.cs"), "w") as f:
        f.write(_build_scene_builder_cs(sprite_meta, any_3d))
    log.append("— wrote Editor/ScratchSceneBuilder.cs (auto-builds the scene on first open; "
               "re-run anytime via Tools > Scratch Import > Build Scene Now)")

    if any(m["has_svg_only_costume"] for m in sprite_meta):
        log.append("⚠ Some sprites only have .svg costumes — Unity can't import those without "
                   "the com.unity.vectorgraphics package. Those sprites are built with no image "
                   "assigned; add one manually or convert the costume to PNG in Scratch first.")

    # --- Project-level files Unity needs to recognize/open the folder ---
    with open(os.path.join(project_settings_dir, "ProjectVersion.txt"), "w") as f:
        f.write(f"m_EditorVersion: {UNITY_VERSION}\nm_EditorVersionWithRevision: {UNITY_VERSION} (0000000000000)\n")

    with open(os.path.join(packages_dir, "manifest.json"), "w") as f:
        json.dump(_PACKAGE_MANIFEST, f, indent=2)

    log.append(f"— wrote ProjectSettings/ProjectVersion.txt (targets Unity {UNITY_VERSION}; "
               "Unity Hub will offer to install/switch if that's not what you have)")
    log.append("— wrote Packages/manifest.json")

    with open(os.path.join(dest_dir, "README_FIRST_OPEN.md"), "w") as f:
        f.write(_project_readme(any_3d, sprite_meta))
    log.append("— wrote README_FIRST_OPEN.md")

    return log


def _extract_filtered(project, assets, dest_dir):
    """Same idea as SB3Project.extract_assets but scoped to one list of
    Asset objects (costumes OR sounds) instead of both at once."""
    import zipfile
    os.makedirs(dest_dir, exist_ok=True)
    extracted = []
    with zipfile.ZipFile(project.path, "r") as z:
        names_in_zip = set(z.namelist())
        for asset in assets:
            if asset.filename and asset.filename in names_in_zip:
                z.extract(asset.filename, dest_dir)
                extracted.append((asset.name, asset.filename))
    return extracted


_PACKAGE_MANIFEST = {
    "dependencies": {
        "com.unity.ugui": "1.0.0",
        "com.unity.modules.audio": "1.0.0",
        "com.unity.modules.imageconversion": "1.0.0",
        "com.unity.modules.imgui": "1.0.0",
        "com.unity.modules.jsonserialize": "1.0.0",
        "com.unity.modules.physics": "1.0.0",
        "com.unity.modules.physics2d": "1.0.0",
        "com.unity.modules.ui": "1.0.0",
        "com.unity.modules.uielements": "1.0.0",
        "com.unity.modules.unitywebrequest": "1.0.0",
        "com.unity.modules.animation": "1.0.0",
    }
}


_TEXTURE_POSTPROCESSOR_CS = '''// Forces anything imported under Assets/Sprites/ to import as a Sprite
// (Unity's default texture import type won't work with SpriteRenderer
// or UI Image otherwise).
using UnityEditor;

public class ScratchTexturePostprocessor : AssetPostprocessor
{
    void OnPreprocessTexture()
    {
        string path = assetPath.Replace('\\\\', '/');
        if (path.Contains("/Assets/Sprites/") || path.StartsWith("Assets/Sprites/"))
        {
            var importer = (TextureImporter)assetImporter;
            importer.textureType = TextureImporterType.Sprite;
            importer.spritePixelsPerUnit = 100f;
        }
    }
}
'''


def _build_scene_builder_cs(sprite_meta, is_3d: bool) -> str:
    spacing = 2.5
    per_sprite_code = []
    for idx, m in enumerate(sprite_meta):
        class_name = m["class_name"]
        object_type = m["object_type"]
        x_pos = idx * spacing

        if object_type == "camera":
            per_sprite_code.append(f'''
        {{
            var go = new GameObject({_csharp_string(class_name)});
            var cam = go.AddComponent<Camera>();
            cam.orthographic = {"false" if is_3d else "true"};
            go.AddComponent<AudioListener>();
            go.AddComponent<{class_name}>();
            go.transform.position = new Vector3({x_pos}f, 1f, {"-10f" if not is_3d else "0f"});
            createdCameras++;
        }}''')
        elif object_type == "ui":
            image_line = (
                f'img.sprite = AssetDatabase.LoadAssetAtPath<Sprite>({_csharp_string(m["costume_rel_path"])});'
                if m["costume_rel_path"] else
                "// no raster costume found for this sprite — assign an Image sprite manually"
            )
            per_sprite_code.append(f'''
        {{
            EnsureCanvas();
            var go = new GameObject({_csharp_string(class_name)}, typeof(RectTransform));
            go.transform.SetParent(canvasTransform, false);
            var rt = go.GetComponent<RectTransform>();
            rt.sizeDelta = new Vector2(160, 60);
            rt.anchoredPosition = new Vector2({x_pos * 40}f, 0f);
            var img = go.AddComponent<UnityEngine.UI.Image>();
            {image_line}
            go.AddComponent<{class_name}>();
            createdUI++;
        }}''')
        else:  # plain GameObject
            image_line = (
                f'sr.sprite = AssetDatabase.LoadAssetAtPath<Sprite>({_csharp_string(m["costume_rel_path"])});'
                if m["costume_rel_path"] else
                "// no raster costume found for this sprite — assign a Sprite manually"
            )
            if is_3d:
                per_sprite_code.append(f'''
        {{
            var go = GameObject.CreatePrimitive(PrimitiveType.Cube);
            go.name = {_csharp_string(class_name)};
            // Placeholder mesh — swap in your real model/prefab; this just
            // gives the script something visible and collidable to sit on.
            go.transform.position = new Vector3({x_pos}f, 0f, 0f);
            go.AddComponent<AudioSource>();
            go.AddComponent<{class_name}>();
            createdObjects++;
        }}''')
            else:
                per_sprite_code.append(f'''
        {{
            var go = new GameObject({_csharp_string(class_name)});
            var sr = go.AddComponent<SpriteRenderer>();
            {image_line}
            go.AddComponent<BoxCollider2D>();
            go.AddComponent<AudioSource>();
            go.AddComponent<{class_name}>();
            go.transform.position = new Vector3({x_pos}f, 0f, 0f);
            createdObjects++;
        }}''')

    sprites_block = "\n".join(per_sprite_code) if per_sprite_code else "        // (no sprites found)"

    return f'''// Auto-generated: builds a starter scene from your converted Scratch
// project. Runs once automatically when this project is first opened
// (via [InitializeOnLoad]); re-run anytime with
// Tools > Scratch Import > Build Scene Now (this always rebuilds, so any
// manual changes you've made to the scene will be lost if you re-run it).
using UnityEngine;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine.SceneManagement;
using System.IO;

[InitializeOnLoad]
public static class ScratchSceneBuilder
{{
    private const string MarkerPath = "Assets/Scripts/Editor/.scratch_scene_built";

    static ScratchSceneBuilder()
    {{
        if (!File.Exists(MarkerPath))
        {{
            EditorApplication.delayCall += () =>
            {{
                BuildScene();
                File.WriteAllText(MarkerPath, "built");
                AssetDatabase.Refresh();
            }};
        }}
    }}

    [MenuItem("Tools/Scratch Import/Build Scene Now")]
    public static void BuildSceneMenuItem()
    {{
        BuildScene();
    }}

    private static Transform canvasTransform;

    private static void EnsureCanvas()
    {{
        if (canvasTransform != null) return;
        var canvasGo = new GameObject("Canvas", typeof(RectTransform));
        var canvas = canvasGo.AddComponent<Canvas>();
        canvas.renderMode = RenderMode.ScreenSpaceOverlay;
        canvasGo.AddComponent<UnityEngine.UI.CanvasScaler>();
        canvasGo.AddComponent<UnityEngine.UI.GraphicRaycaster>();
        canvasTransform = canvasGo.transform;

        if (Object.FindObjectOfType<UnityEngine.EventSystems.EventSystem>() == null)
        {{
            var esGo = new GameObject("EventSystem");
            esGo.AddComponent<UnityEngine.EventSystems.EventSystem>();
            esGo.AddComponent<UnityEngine.EventSystems.StandaloneInputModule>();
        }}
    }}

    private static void BuildScene()
    {{
        try
        {{
            canvasTransform = null;
            int createdObjects = 0, createdUI = 0, createdCameras = 0;

            var scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);

{sprites_block}

            if (createdCameras == 0)
            {{
                var camGo = new GameObject("Main Camera");
                var cam = camGo.AddComponent<Camera>();
                cam.orthographic = {"false" if is_3d else "true"};
                camGo.AddComponent<AudioListener>();
                camGo.tag = "MainCamera";
                camGo.transform.position = new Vector3(0f, 0f, -10f);
            }}

            if (!Directory.Exists("Assets/Scenes")) Directory.CreateDirectory("Assets/Scenes");
            EditorSceneManager.SaveScene(scene, "Assets/Scenes/ScratchScene.unity");
            EditorBuildSettings.scenes = new[] {{ new EditorBuildSettingsScene("Assets/Scenes/ScratchScene.unity", true) }};

            Debug.Log($"[Scratch Import] Scene built: {{createdObjects}} object(s), {{createdUI}} UI element(s), {{createdCameras}} camera(s). " +
                      "Re-run via Tools > Scratch Import > Build Scene Now if you need to regenerate it.");
        }}
        catch (System.Exception ex)
        {{
            Debug.LogError("[Scratch Import] Scene build failed: " + ex);
        }}
    }}
}}
'''


def _project_readme(is_3d: bool, sprite_meta) -> str:
    lines = [
        "# Generated Unity project — first open",
        "",
        f"Targets Unity **{UNITY_VERSION}**. If that's not installed, Unity Hub will",
        "offer to install it or open with whatever you do have — either is fine,",
        "this project doesn't use anything version-specific.",
        "",
        "## What happens automatically",
        "",
        "The first time you open this project, an Editor script "
        "(`Assets/Scripts/Editor/ScratchSceneBuilder.cs`) builds a starter scene:",
        "one GameObject per sprite (with its converted script attached), a shared",
        "Canvas for anything marked as UI, and a Main Camera if no sprite claimed",
        "the camera role itself. It only auto-runs once — re-run it anytime from",
        "**Tools > Scratch Import > Build Scene Now** (this rebuilds from scratch,",
        "so it'll discard any manual changes you made to the scene).",
        "",
        "## Object roles detected from your \"Object type: ()\" custom block",
        "",
    ]
    for m in sprite_meta:
        lines.append(f"- **{m['sprite_name']}** → {m['object_type']}")
    lines += [
        "",
        "Sprites with no \"Object type\" call default to a regular GameObject",
        "(SpriteRenderer in 2D projects, a placeholder Cube in 3D — swap that",
        "mesh for your real model).",
        "",
        "## Known rough edges",
        "",
        "- Only PNG/JPG costumes get auto-assigned to the generated objects.",
        "  SVG costumes (Scratch's default vector format) aren't importable by",
        "  stock Unity — export those costumes as PNG from Scratch first if you",
        "  want them auto-wired.",
        "- UI sprites use `RectTransform`, but the converted scripts still write",
        "  to `transform.position` like a normal sprite — Scratch's coordinate",
        "  blocks won't move a UI element correctly out of the box. Worth a",
        "  manual pass if you're leaning on UI heavily.",
        "- The Camera role attaches the sprite's script to the actual Camera",
        "  object, which is usually fine for e.g. a follow-player script, but",
        "  motion blocks like costume-switching obviously won't do anything",
        "  useful on a camera.",
    ]
    return "\n".join(lines)
