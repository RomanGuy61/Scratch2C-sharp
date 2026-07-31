import os
import traceback

import flet as ft

from sb3_parser import SB3Project
from script_builder import generate_csharp, get_base_class_template, build_class_name, count_coverage
from unity_project_builder import generate_unity_project


async def main(page: ft.Page):
    page.title = "Scratch (.sb3) to Unity C# Converter"
    page.window.width = 940
    page.window.height = 760
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 20

    state = {"project": None, "sprite_name": None, "sb3_path": None, "is_3d": False}

    status_text = ft.Text("Select a .sb3 project to begin.", size=14, italic=True)
    sprite_dropdown = ft.Dropdown(label="Sprite", options=[], disabled=True, width=260)
    convert_button = ft.ElevatedButton("Convert to C#", icon=ft.Icons.CODE, disabled=True)
    save_button = ft.ElevatedButton("Save .cs + assets", icon=ft.Icons.SAVE, disabled=True)
    convert_all_button = ft.ElevatedButton("Convert All Sprites…", icon=ft.Icons.LAYERS, disabled=True)
    unity_project_button = ft.ElevatedButton("Generate Unity Project…", icon=ft.Icons.SPORTS_ESPORTS, disabled=True)
    code_view = ft.TextField(
        value="", multiline=True, read_only=True, min_lines=22, max_lines=22,
        text_size=12, text_style=ft.TextStyle(font_family="monospace"),
        border_color=ft.Colors.OUTLINE,
    )
    sprite_info = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
    coverage_text = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
    batch_log = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT, selectable=True)

    file_picker = ft.FilePicker()
    save_picker = ft.FilePicker()
    batch_picker = ft.FilePicker()
    unity_picker = ft.FilePicker()

    def load_project(path: str):
        try:
            project = SB3Project(path)
        except Exception as ex:
            status_text.value = f"Failed to open project: {ex}"
            status_text.color = ft.Colors.RED_300
            page.update()
            return

        sprites = project.non_stage_sprites()
        if not sprites:
            status_text.value = "No sprites found in this project."
            status_text.color = ft.Colors.RED_300
            page.update()
            return

        state["project"] = project
        state["sb3_path"] = path
        state["sprite_name"] = None

        sprite_dropdown.options = [ft.dropdown.Option(s.name) for s in sprites]
        sprite_dropdown.value = None
        sprite_dropdown.disabled = False
        convert_button.disabled = True
        convert_all_button.disabled = False
        unity_project_button.disabled = False
        save_button.disabled = True
        code_view.value = ""
        sprite_info.value = ""
        coverage_text.value = ""
        batch_log.value = ""
        status_text.value = f"Loaded '{os.path.basename(path)}' — found {len(sprites)} sprite(s). Pick one below, or convert all at once."
        status_text.color = ft.Colors.GREEN_300
        page.update()

    async def on_pick_project(e):
        files = await file_picker.pick_files(
            dialog_title="Select a Scratch project",
            allowed_extensions=["sb3"],
        )
        if files:
            load_project(files[0].path)

    def on_sprite_selected(e):
        state["sprite_name"] = sprite_dropdown.value
        convert_button.disabled = state["sprite_name"] is None
        save_button.disabled = True
        code_view.value = ""
        coverage_text.value = ""
        if state["sprite_name"]:
            sprite = state["project"].get_sprite(state["sprite_name"])
            n_scripts = len([b for b in sprite.blocks.values()
                              if isinstance(b, dict) and b.get("topLevel") and b.get("parent") is None])
            sprite_info.value = (f"{sprite.name}: {len(sprite.blocks)} blocks, {n_scripts} script(s), "
                                  f"{len(sprite.costume_assets)} costume(s), {len(sprite.sound_assets)} sound(s), "
                                  f"{len(sprite.variables)} variable(s), {len(sprite.lists)} list(s)")
        else:
            sprite_info.value = ""
        page.update()

    def on_convert(e):
        try:
            sprite = state["project"].get_sprite(state["sprite_name"])
            code, is_3d = generate_csharp(sprite, state["project"])
            code_view.value = code
            state["is_3d"] = is_3d
            save_button.disabled = False
            mode = "3D (Vector3)" if is_3d else "2D (Vector2, default)"
            flagged, total = count_coverage(code)
            clean = total - flagged
            coverage_text.value = (f"{clean}/{total} generated lines converted cleanly — "
                                    f"{flagged} flagged with TODO/not-implemented for manual follow-up."
                                    if total else "")
            status_text.value = f"Converted '{sprite.name}' — detected project type: {mode}."
            status_text.color = ft.Colors.GREEN_300
        except Exception as ex:
            code_view.value = f"// Conversion failed:\n// {ex}\n\n" + traceback.format_exc()
            status_text.value = "Conversion failed — see output for details."
            status_text.color = ft.Colors.RED_300
        page.update()

    async def on_save_click(e):
        dir_path = await save_picker.get_directory_path(
            dialog_title="Choose a folder to save the .cs file(s)"
        )
        if not dir_path:
            return
        sprite = state["project"].get_sprite(state["sprite_name"])
        class_name = build_class_name(sprite.name)
        try:
            with open(os.path.join(dir_path, f"{class_name}.cs"), "w") as f:
                f.write(code_view.value)
            base_path = os.path.join(dir_path, "ScratchSpriteBase.cs")
            with open(base_path, "w") as f:
                f.write(get_base_class_template(state.get("is_3d", False)))
            extracted = state["project"].extract_assets(
                sprite, os.path.join(dir_path, "Assets", class_name)
            )
            msg = f"Saved {class_name}.cs and ScratchSpriteBase.cs to {dir_path}"
            if extracted:
                msg += f", plus {len(extracted)} asset file(s) under Assets/{class_name}/"
            status_text.value = msg
            status_text.color = ft.Colors.GREEN_300
        except Exception as ex:
            status_text.value = f"Save failed: {ex}"
            status_text.color = ft.Colors.RED_300
        page.update()

    async def on_convert_all_click(e):
        dir_path = await batch_picker.get_directory_path(
            dialog_title="Choose a folder for the whole-project conversion"
        )
        if not dir_path:
            return
        project = state["project"]
        sprites = project.non_stage_sprites()
        lines = []
        any_3d = False
        total_flagged = total_lines = 0
        for sprite in sprites:
            try:
                code, is_3d = generate_csharp(sprite, project)
                any_3d = any_3d or is_3d
                class_name = build_class_name(sprite.name)
                with open(os.path.join(dir_path, f"{class_name}.cs"), "w") as f:
                    f.write(code)
                extracted = project.extract_assets(sprite, os.path.join(dir_path, "Assets", class_name))
                flagged, total = count_coverage(code)
                total_flagged += flagged
                total_lines += total
                lines.append(f"✓ {sprite.name} → {class_name}.cs  "
                              f"({total - flagged}/{total} clean, {len(extracted)} asset file(s))")
            except Exception as ex:
                lines.append(f"✗ {sprite.name} FAILED: {ex}")
        try:
            with open(os.path.join(dir_path, "ScratchSpriteBase.cs"), "w") as f:
                f.write(get_base_class_template(any_3d))
            lines.append(f"— wrote shared ScratchSpriteBase.cs ({'3D' if any_3d else '2D'} — "
                          f"if sprites disagreed on Project Type, 3D was preferred)")
        except Exception as ex:
            lines.append(f"✗ Failed to write ScratchSpriteBase.cs: {ex}")

        batch_log.value = "\n".join(lines)
        status_text.value = (f"Converted {len(sprites)} sprite(s) to {dir_path} — "
                              f"{total_lines - total_flagged}/{total_lines} lines clean overall.")
        status_text.color = ft.Colors.GREEN_300
        page.update()

    async def on_unity_project_click(e):
        dir_path = await unity_picker.get_directory_path(
            dialog_title="Choose a folder for the new Unity project (should be empty/new)"
        )
        if not dir_path:
            return
        try:
            log_lines = generate_unity_project(state["project"], dir_path)
            batch_log.value = "\n".join(log_lines)
            status_text.value = (f"Generated a Unity project at {dir_path} — open it in Unity/Unity Hub; "
                                  f"the scene builds itself on first load. See README_FIRST_OPEN.md there for details.")
            status_text.color = ft.Colors.GREEN_300
        except Exception as ex:
            batch_log.value = f"Unity project generation failed: {ex}\n" + traceback.format_exc()
            status_text.value = "Unity project generation failed — see log below."
            status_text.color = ft.Colors.RED_300
        page.update()

    sprite_dropdown.on_select = on_sprite_selected
    convert_button.on_click = on_convert
    save_button.on_click = on_save_click
    convert_all_button.on_click = on_convert_all_click
    unity_project_button.on_click = on_unity_project_click

    pick_button = ft.ElevatedButton(
        "Select .sb3 project", icon=ft.Icons.FOLDER_OPEN,
        on_click=on_pick_project,
    )

    page.add(
        ft.Column(
            [
                ft.Text("Scratch → Unity C# Converter", size=24, weight=ft.FontWeight.BOLD),
                ft.Text(
                    "Pick a .sb3 file, choose a sprite, and get a best-effort Unity "
                    "MonoBehaviour translation of its scripts — costumes and sounds are "
                    "extracted alongside the code, 'Convert All Sprites' does the whole "
                    "project in one pass, and 'Generate Unity Project' scaffolds an "
                    "actual openable Unity project with the scene built for you.",
                    size=13, color=ft.Colors.ON_SURFACE_VARIANT,
                ),
                ft.Row([pick_button, sprite_dropdown, convert_button, save_button, convert_all_button, unity_project_button]),
                status_text,
                sprite_info,
                coverage_text,
                batch_log,
                ft.Divider(),
                code_view,
            ],
            spacing=14,
            expand=True,
            scroll=ft.ScrollMode.AUTO,
        )
    )


if __name__ == "__main__":
    ft.run(main)
