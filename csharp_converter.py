"""
Converts a Scratch sprite's blocks into a best-effort Unity C# MonoBehaviour.

Scratch's block graph is walked recursively:
 - "hat" blocks (top-level, no parent) become event entry points.
 - "next" chains become sequential statements.
 - "inputs" that hold another block are reporters, converted to C# expressions.
 - "substack" inputs (loop/if bodies) are recursed into indented blocks.

Coverage is intentionally focused on the ~90% of opcodes people actually use.
Anything unrecognized is emitted as a commented TODO with its opcode and raw
inputs/fields so a human can finish the translation by hand.
"""
import re


def sanitize_ident(name: str) -> str:
    name = re.sub(r"[^0-9a-zA-Z_]", "_", name.strip())
    if not name:
        name = "_"
    if name[0].isdigit():
        name = "_" + name
    return name


def csharp_string_literal(s: str) -> str:
    s = str(s)
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


class ConversionContext:
    """Holds per-sprite state needed while converting: variable/list names,
    broadcast names, custom-block signatures, and which event handlers /
    coroutines / broadcast-receiver methods have been generated."""

    def __init__(self, sprite, project):
        self.sprite = sprite
        self.project = project
        self.blocks = sprite.blocks

        # Global (stage) vars/lists are visible to all sprites in Scratch;
        # merge them in so references resolve.
        self.variables = {}
        self.lists = {}
        self.broadcasts = {}
        stage = next((s for s in project.sprites if s.is_stage), None)
        if stage:
            self.variables.update(stage.variables)
            self.lists.update(stage.lists)
            self.broadcasts.update(stage.broadcasts)
        self.variables.update(sprite.variables)
        self.lists.update(sprite.lists)
        self.broadcasts.update(sprite.broadcasts)

        self.var_names = {vid: sanitize_ident(v[0]) for vid, v in self.variables.items()}
        self.list_names = {lid: sanitize_ident(v[0]) for lid, v in self.lists.items()}
        self.broadcast_names = {bid: sanitize_ident(v[0]) for bid, v in self.broadcasts.items()}

        # A variable named "Project Type" set to "Vector2" or "Vector3" tells
        # us whether to generate 2D-plane or 3D-plane motion code. Defaults
        # to 2D (Scratch's native coordinate system) if not found.
        self.project_type_var_id = None
        self.is_3d = False
        for vid, v in self.variables.items():
            if len(v) >= 1 and str(v[0]).strip().lower() == "project type":
                self.project_type_var_id = vid
                value = str(v[1]).strip() if len(v) >= 2 else ""
                self.is_3d = (value == "Vector3")
                break

        self.coroutine_counter = 0
        self.extra_methods = []  # list of (signature_lines) generated for waits/loops needing coroutines
        self.uses_coroutines = False
        self.custom_blocks = {}  # proccode -> (method name, arg names in order)
        self._scan_custom_blocks()

    def _scan_custom_blocks(self):
        for bid, block in self.blocks.items():
            if not isinstance(block, dict):
                continue
            if block.get("opcode") == "procedures_prototype":
                mutation = block.get("mutation", {})
                proccode = mutation.get("proccode", "")
                argnames_raw = mutation.get("argumentnames", "[]")
                try:
                    import json as _json
                    argnames = _json.loads(argnames_raw) if isinstance(argnames_raw, str) else argnames_raw
                except Exception:
                    argnames = []
                # proccode looks like "my block %s and %b" -> strip params for a method name
                base = re.sub(r"%[snb]", "", proccode).strip()
                method_name = sanitize_ident(base) or "CustomBlock"
                method_name = method_name[0].upper() + method_name[1:] if method_name else "CustomBlock"
                self.custom_blocks[proccode] = (method_name, [sanitize_ident(a) for a in argnames])

    def new_coroutine_name(self):
        self.coroutine_counter += 1
        return f"Routine_{self.coroutine_counter}"


HAT_OPCODES = {
    "event_whenflagclicked": "flag",
    "event_whenkeypressed": "key",
    "event_whenthisspriteclicked": "clicked",
    "event_whenbroadcastreceived": "broadcast",
    "event_whenbackdropswitchesto": "backdrop",
    "event_whengreaterthan": "greaterthan",
    "control_start_as_clone": "clone",
    "procedures_definition": "procedure",
}


def get_input_block_id(block, name):
    """Return the block id plugged into an input slot, or None."""
    inp = block.get("inputs", {}).get(name)
    if not inp:
        return None
    if inp[0] in (2, 3) and isinstance(inp[1], str):
        return inp[1]
    return None


def get_input_primitive(block, name, default="0"):
    """Return a literal value for an input slot that isn't a block (or the
    shadow default when nothing is plugged in)."""
    inp = block.get("inputs", {}).get(name)
    if not inp:
        return default
    value = inp[1]
    if isinstance(value, list):
        # [type, val] or [type, name, id]
        if len(value) >= 2:
            return value[1]
    return default


def get_field(block, name, default=""):
    f = block.get("fields", {}).get(name)
    if not f:
        return default
    return f[0]


class BlockConverter:
    def __init__(self, ctx: ConversionContext):
        self.ctx = ctx
        self.blocks = ctx.blocks

    # ---------- Expressions (reporters) ----------

    def expr(self, block_id):
        if block_id is None:
            return "0"
        block = self.blocks.get(block_id)
        if not block:
            return "0"
        op = block["opcode"]
        method = getattr(self, f"expr_{op}", None)
        if method:
            try:
                return method(block)
            except Exception as e:
                return f'/* error converting {op}: {e} */ 0'
        return f'/* TODO reporter: {op} */ 0'

    def input_expr(self, block, name, fallback="0"):
        bid = get_input_block_id(block, name)
        if bid:
            return self.expr(bid)
        prim = get_input_primitive(block, name, fallback)
        # numeric-looking primitives: emit as-is; else string literal
        if isinstance(prim, (int, float)):
            return str(prim)
        s = str(prim)
        try:
            float(s)
            return s
        except ValueError:
            return csharp_string_literal(s)

    # --- Motion reporters ---
    def expr_motion_xposition(self, b): return "transform.position.x"
    def expr_motion_yposition(self, b): return "transform.position.z" if self.ctx.is_3d else "transform.position.y"
    def expr_motion_direction(self, b): return "facingDirection"

    # --- Looks reporters ---
    def expr_looks_costumenumbername(self, b):
        opt = get_field(b, "NUMBER_NAME", "number")
        return "currentCostumeIndex + 1" if opt == "number" else "currentCostumeName"

    def expr_looks_backdropnumbername(self, b):
        opt = get_field(b, "NUMBER_NAME", "number")
        return "Stage.CurrentBackdropIndex + 1" if opt == "number" else "Stage.CurrentBackdropName"

    def expr_looks_size(self, b): return "(transform.localScale.x * 100f)"

    # --- Sound reporters ---
    def expr_sound_volume(self, b): return "audioSource.volume * 100f"

    # --- Sensing reporters ---
    def expr_sensing_touchingobject(self, b):
        opt = get_field(b, "TOUCHINGOBJECTMENU", "_mouse_")
        if opt == "_mouse_":
            return "IsTouchingMouse()"
        if opt == "_edge_":
            return "IsTouchingEdge()"
        return f'IsTouchingObject({csharp_string_literal(opt)})'

    def expr_sensing_touchingcolor(self, b): return "false /* touching color: not implemented */"
    def expr_sensing_coloristouchingcolor(self, b): return "false /* color touching color: not implemented */"
    def expr_sensing_distanceto(self, b):
        opt = get_field(b, "DISTANCETOMENU", "_mouse_")
        target = "MousePosition()" if opt == "_mouse_" else f'FindSprite({csharp_string_literal(opt)}).transform.position'
        return f"Vector3.Distance(transform.position, {target})"

    def expr_sensing_answer(self, b): return "answer"
    def expr_sensing_keypressed(self, b):
        key = get_field(b, "KEY_OPTION", "space")
        return f"Input.GetKey(KeyCode.{scratch_key_to_unity(key)})"
    def expr_sensing_mousedown(self, b): return "Input.GetMouseButton(0)"
    def expr_sensing_mousex(self, b): return "MousePosition().x"
    def expr_sensing_mousey(self, b): return "MousePosition().y"
    def expr_sensing_loudness(self, b): return "GetLoudness()"
    def expr_sensing_timer(self, b): return "scratchTimer"
    def expr_sensing_of(self, b):
        prop = get_field(b, "PROPERTY", "x position")
        return f'/* "{prop}" of object: not implemented */ 0'
    def expr_sensing_current(self, b):
        opt = get_field(b, "CURRENTMENU", "YEAR")
        mapping = {
            "YEAR": "DateTime.Now.Year", "MONTH": "DateTime.Now.Month",
            "DATE": "DateTime.Now.Day", "DAYOFWEEK": "(int)DateTime.Now.DayOfWeek",
            "HOUR": "DateTime.Now.Hour", "MINUTE": "DateTime.Now.Minute",
            "SECOND": "DateTime.Now.Second",
        }
        return mapping.get(opt, "0")
    def expr_sensing_dayssince2000(self, b): return "(DateTime.Now - new DateTime(2000,1,1)).TotalDays"
    def expr_sensing_username(self, b): return '"Player"'

    # --- Operators ---
    def expr_operator_add(self, b): return f'({self.input_expr(b,"NUM1")} + {self.input_expr(b,"NUM2")})'
    def expr_operator_subtract(self, b): return f'({self.input_expr(b,"NUM1")} - {self.input_expr(b,"NUM2")})'
    def expr_operator_multiply(self, b): return f'({self.input_expr(b,"NUM1")} * {self.input_expr(b,"NUM2")})'
    def expr_operator_divide(self, b): return f'({self.input_expr(b,"NUM1")} / {self.input_expr(b,"NUM2")})'
    def expr_operator_random(self, b):
        return f'ScratchRandom({self.input_expr(b,"FROM")}, {self.input_expr(b,"TO")})'
    def expr_operator_gt(self, b): return f'(System.Convert.ToDouble({self.input_expr(b,"OPERAND1")}) > System.Convert.ToDouble({self.input_expr(b,"OPERAND2")}))'
    def expr_operator_lt(self, b): return f'(System.Convert.ToDouble({self.input_expr(b,"OPERAND1")}) < System.Convert.ToDouble({self.input_expr(b,"OPERAND2")}))'
    def expr_operator_equals(self, b): return f'Equals({self.input_expr(b,"OPERAND1")}, {self.input_expr(b,"OPERAND2")})'
    def expr_operator_and(self, b): return f'({self.input_expr(b,"OPERAND1")} && {self.input_expr(b,"OPERAND2")})'
    def expr_operator_or(self, b): return f'({self.input_expr(b,"OPERAND1")} || {self.input_expr(b,"OPERAND2")})'
    def expr_operator_not(self, b): return f'!({self.input_expr(b,"OPERAND")})'
    def expr_operator_join(self, b): return f'({self.input_expr(b,"STRING1")}.ToString() + {self.input_expr(b,"STRING2")}.ToString())'
    def expr_operator_letter_of(self, b):
        return f'ScratchLetterOf({self.input_expr(b,"LETTER")}, {self.input_expr(b,"STRING")}.ToString())'
    def expr_operator_length(self, b): return f'{self.input_expr(b,"STRING")}.ToString().Length'
    def expr_operator_contains(self, b):
        return f'{self.input_expr(b,"STRING1")}.ToString().ToLower().Contains({self.input_expr(b,"STRING2")}.ToString().ToLower())'
    def expr_operator_mod(self, b): return f'ScratchMod({self.input_expr(b,"NUM1")}, {self.input_expr(b,"NUM2")})'
    def expr_operator_round(self, b): return f'Mathf.Round((float){self.input_expr(b,"NUM")})'
    def expr_operator_mathop(self, b):
        opt = get_field(b, "OPERATOR", "abs")
        v = self.input_expr(b, "NUM")
        mapping = {
            "abs": f"Mathf.Abs((float){v})", "floor": f"Mathf.Floor((float){v})",
            "ceiling": f"Mathf.Ceil((float){v})", "sqrt": f"Mathf.Sqrt((float){v})",
            "sin": f"Mathf.Sin((float){v} * Mathf.Deg2Rad)", "cos": f"Mathf.Cos((float){v} * Mathf.Deg2Rad)",
            "tan": f"Mathf.Tan((float){v} * Mathf.Deg2Rad)",
            "asin": f"(Mathf.Asin((float){v}) * Mathf.Rad2Deg)", "acos": f"(Mathf.Acos((float){v}) * Mathf.Rad2Deg)",
            "atan": f"(Mathf.Atan((float){v}) * Mathf.Rad2Deg)",
            "ln": f"Mathf.Log((float){v})", "log": f"Mathf.Log10((float){v})",
            "e ^": f"Mathf.Exp((float){v})", "10 ^": f"Mathf.Pow(10f, (float){v})",
        }
        return mapping.get(opt, f"0 /* mathop {opt} */")

    # --- Variables / Lists reporters ---
    def expr_data_variable(self, b):
        vid = b.get("fields", {}).get("VARIABLE", ["var"])[1] if len(b.get("fields", {}).get("VARIABLE", [])) > 1 else None
        name = self.ctx.var_names.get(vid, sanitize_ident(get_field(b, "VARIABLE", "myVar")))
        return name

    def expr_data_listcontents(self, b):
        lid = b.get("fields", {}).get("LIST", ["list"])[1] if len(b.get("fields", {}).get("LIST", [])) > 1 else None
        name = self.ctx.list_names.get(lid, sanitize_ident(get_field(b, "LIST", "myList")))
        return f'string.Join(" ", {name})'

    def expr_data_itemoflist(self, b):
        lid = self._list_id_from_field(b)
        name = self.ctx.list_names.get(lid, "myList")
        idx = self.input_expr(b, "INDEX", "1")
        return f'{name}[(int){idx} - 1]'

    def expr_data_itemnumoflist(self, b):
        lid = self._list_id_from_field(b)
        name = self.ctx.list_names.get(lid, "myList")
        item = self.input_expr(b, "ITEM")
        return f'({name}.IndexOf({item}) + 1)'

    def expr_data_lengthoflist(self, b):
        lid = self._list_id_from_field(b)
        name = self.ctx.list_names.get(lid, "myList")
        return f'{name}.Count'

    def expr_data_listcontainsitem(self, b):
        lid = self._list_id_from_field(b)
        name = self.ctx.list_names.get(lid, "myList")
        item = self.input_expr(b, "ITEM")
        return f'{name}.Contains({item})'

    def expr_argument_reporter_string_number(self, b):
        return sanitize_ident(get_field(b, "VALUE", "arg"))

    def expr_argument_reporter_boolean(self, b):
        return sanitize_ident(get_field(b, "VALUE", "arg"))

    def _list_id_from_field(self, b):
        f = b.get("fields", {}).get("LIST")
        if f and len(f) > 1:
            return f[1]
        return None

    def _var_id_from_field(self, b, key="VARIABLE"):
        f = b.get("fields", {}).get(key)
        if f and len(f) > 1:
            return f[1]
        return None

    # ---------- Statements ----------

    def statement_chain(self, first_block_id, indent=1):
        lines = []
        bid = first_block_id
        seen = set()
        while bid and bid not in seen:
            seen.add(bid)
            block = self.blocks.get(bid)
            if not block:
                break
            lines.extend(self.statement(block, indent))
            bid = block.get("next")
        return lines

    def substack(self, block, name, indent):
        bid = get_input_block_id(block, name)
        if not bid:
            return [self._pad(indent) + "// (empty)"]
        return self.statement_chain(bid, indent)

    def _pad(self, indent):
        return "    " * indent

    def statement(self, block, indent=1):
        op = block["opcode"]
        method = getattr(self, f"stmt_{op}", None)
        pad = self._pad(indent)
        if method:
            try:
                out = method(block, indent)
                return out if out is not None else []
            except Exception as e:
                return [f"{pad}// error converting {op}: {e}"]
        return [f"{pad}// TODO statement: {op}  fields={block.get('fields')} inputs={list(block.get('inputs', {}).keys())}"]

    # --- Motion statements ---
    def stmt_motion_movesteps(self, b, i):
        steps = self.input_expr(b, "STEPS", "10")
        return [f"{self._pad(i)}MoveSteps((float)({steps}));"]

    def stmt_motion_turnright(self, b, i):
        deg = self.input_expr(b, "DEGREES", "15")
        return [f"{self._pad(i)}facingDirection += (float)({deg}); ApplyFacingRotation();"]

    def stmt_motion_turnleft(self, b, i):
        deg = self.input_expr(b, "DEGREES", "15")
        return [f"{self._pad(i)}facingDirection -= (float)({deg}); ApplyFacingRotation();"]

    def stmt_motion_goto(self, b, i):
        opt = get_field(b, "TO", "_random_")
        if opt == "_random_":
            return [f"{self._pad(i)}transform.position = RandomStagePosition();"]
        if opt == "_mouse_":
            return [f"{self._pad(i)}{{ var m = MousePosition(); GotoXY(m.x, LogicalY(m)); }}"]
        return [f"{self._pad(i)}{{ var t = FindSprite({csharp_string_literal(opt)}); GotoXY(t.transform.position.x, LogicalY(t.transform.position)); }}"]

    def stmt_motion_gotoxy(self, b, i):
        x = self.input_expr(b, "X", "0")
        y = self.input_expr(b, "Y", "0")
        return [f"{self._pad(i)}GotoXY((float)({x}), (float)({y}));"]

    def stmt_motion_glideto(self, b, i):
        secs = self.input_expr(b, "SECS", "1")
        opt = get_field(b, "TO", "_random_")
        target = "MousePosition()" if opt == "_mouse_" else (
            f'FindSprite({csharp_string_literal(opt)}).transform.position' if opt != "_random_"
            else "RandomStagePosition()")
        self.ctx.uses_coroutines = True
        return [f"{self._pad(i)}yield return StartCoroutine(GlideTo({target}, (float)({secs})));"]

    def stmt_motion_glidesecstoxy(self, b, i):
        secs = self.input_expr(b, "SECS", "1")
        x = self.input_expr(b, "X", "0")
        y = self.input_expr(b, "Y", "0")
        self.ctx.uses_coroutines = True
        return [f"{self._pad(i)}yield return StartCoroutine(GlideTo(MakePosition((float)({x}),(float)({y})), (float)({secs})));"]

    def stmt_motion_pointindirection(self, b, i):
        dir_ = self.input_expr(b, "DIRECTION", "90")
        return [f"{self._pad(i)}facingDirection = (float)({dir_}); ApplyFacingRotation();"]

    def stmt_motion_pointtowards(self, b, i):
        opt = get_field(b, "TOWARDS", "_mouse_")
        target = "MousePosition()" if opt == "_mouse_" else f'FindSprite({csharp_string_literal(opt)}).transform.position'
        return [f"{self._pad(i)}PointTowards({target});"]

    def stmt_motion_changexby(self, b, i):
        v = self.input_expr(b, "DX", "10")
        return [f"{self._pad(i)}ChangeX((float)({v}));"]

    def stmt_motion_setx(self, b, i):
        v = self.input_expr(b, "X", "0")
        return [f"{self._pad(i)}SetX((float)({v}));"]

    def stmt_motion_changeyby(self, b, i):
        v = self.input_expr(b, "DY", "10")
        return [f"{self._pad(i)}ChangeY((float)({v}));"]

    def stmt_motion_sety(self, b, i):
        v = self.input_expr(b, "Y", "0")
        return [f"{self._pad(i)}SetY((float)({v}));"]

    def stmt_motion_ifonedgebounce(self, b, i):
        return [f"{self._pad(i)}BounceOffEdge();"]

    def stmt_motion_setrotationstyle(self, b, i):
        style = get_field(b, "STYLE", "all around")
        return [f"{self._pad(i)}rotationStyle = {csharp_string_literal(style)};"]

    # --- Looks statements ---
    def stmt_looks_sayforsecs(self, b, i):
        msg = self.input_expr(b, "MESSAGE", '""')
        secs = self.input_expr(b, "SECS", "2")
        self.ctx.uses_coroutines = True
        return [f'{self._pad(i)}Say({msg}.ToString()); yield return new WaitForSeconds((float)({secs})); Say("");']

    def stmt_looks_say(self, b, i):
        msg = self.input_expr(b, "MESSAGE", '""')
        return [f'{self._pad(i)}Say({msg}.ToString());']

    def stmt_looks_thinkforsecs(self, b, i):
        msg = self.input_expr(b, "MESSAGE", '""')
        secs = self.input_expr(b, "SECS", "2")
        self.ctx.uses_coroutines = True
        return [f'{self._pad(i)}Think({msg}.ToString()); yield return new WaitForSeconds((float)({secs})); Think("");']

    def stmt_looks_think(self, b, i):
        msg = self.input_expr(b, "MESSAGE", '""')
        return [f'{self._pad(i)}Think({msg}.ToString());']

    def stmt_looks_switchcostumeto(self, b, i):
        opt = get_field(b, "COSTUME", "costume1")
        return [f"{self._pad(i)}SetCostume({csharp_string_literal(opt)});"]

    def stmt_looks_nextcostume(self, b, i):
        return [f"{self._pad(i)}NextCostume();"]

    def stmt_looks_switchbackdropto(self, b, i):
        opt = get_field(b, "BACKDROP", "backdrop1")
        return [f"{self._pad(i)}Stage.SetBackdrop({csharp_string_literal(opt)});"]

    def stmt_looks_nextbackdrop(self, b, i):
        return [f"{self._pad(i)}Stage.NextBackdrop();"]

    def stmt_looks_changesizeby(self, b, i):
        v = self.input_expr(b, "CHANGE", "10")
        return [f"{self._pad(i)}ChangeSizeBy((float)({v}));"]

    def stmt_looks_setsizeto(self, b, i):
        v = self.input_expr(b, "SIZE", "100")
        return [f"{self._pad(i)}SetSize((float)({v}));"]

    def stmt_looks_changeeffectby(self, b, i):
        eff = get_field(b, "EFFECT", "COLOR")
        v = self.input_expr(b, "CHANGE", "25")
        return [f"{self._pad(i)}ChangeGraphicEffect({csharp_string_literal(eff)}, (float)({v}));"]

    def stmt_looks_seteffectto(self, b, i):
        eff = get_field(b, "EFFECT", "COLOR")
        v = self.input_expr(b, "VALUE", "0")
        return [f"{self._pad(i)}SetGraphicEffect({csharp_string_literal(eff)}, (float)({v}));"]

    def stmt_looks_cleargraphiceffects(self, b, i):
        return [f"{self._pad(i)}ClearGraphicEffects();"]

    def stmt_looks_show(self, b, i):
        return [f"{self._pad(i)}SetVisible(true);"]

    def stmt_looks_hide(self, b, i):
        return [f"{self._pad(i)}SetVisible(false);"]

    def stmt_looks_gotofrontback(self, b, i):
        opt = get_field(b, "FRONT_BACK", "front")
        return [f"{self._pad(i)}SetLayerOrder({'int.MaxValue' if opt=='front' else 'int.MinValue'});"]

    def stmt_looks_goforwardbackwardlayers(self, b, i):
        opt = get_field(b, "FORWARD_BACKWARD", "forward")
        num = self.input_expr(b, "NUM", "1")
        sign = "" if opt == "forward" else "-"
        return [f"{self._pad(i)}ChangeLayerOrder({sign}(int)({num}));"]

    # --- Sound statements ---
    def stmt_sound_playuntildone(self, b, i):
        opt = get_field(b, "SOUND_MENU", "")
        self.ctx.uses_coroutines = True
        return [f"{self._pad(i)}yield return StartCoroutine(PlaySoundUntilDone({csharp_string_literal(opt)}));"]

    def stmt_sound_play(self, b, i):
        opt = get_field(b, "SOUND_MENU", "")
        return [f"{self._pad(i)}PlaySound({csharp_string_literal(opt)});"]

    def stmt_sound_stopallsounds(self, b, i):
        return [f"{self._pad(i)}StopAllSounds();"]

    def stmt_sound_changevolumeby(self, b, i):
        v = self.input_expr(b, "VOLUME", "10")
        return [f"{self._pad(i)}audioSource.volume = Mathf.Clamp01(audioSource.volume + (float)({v})/100f);"]

    def stmt_sound_setvolumeto(self, b, i):
        v = self.input_expr(b, "VOLUME", "100")
        return [f"{self._pad(i)}audioSource.volume = Mathf.Clamp01((float)({v})/100f);"]

    # --- Events statements ---
    def stmt_event_broadcast(self, b, i):
        name = self._broadcast_name(b, "BROADCAST_INPUT")
        return [f"{self._pad(i)}Broadcast({csharp_string_literal(name)});"]

    def stmt_event_broadcastandwait(self, b, i):
        name = self._broadcast_name(b, "BROADCAST_INPUT")
        self.ctx.uses_coroutines = True
        return [f"{self._pad(i)}yield return StartCoroutine(BroadcastAndWait({csharp_string_literal(name)}));"]

    def _broadcast_name(self, b, input_name):
        inp = b.get("inputs", {}).get(input_name)
        if inp and isinstance(inp[1], list) and len(inp[1]) >= 3:
            bid = inp[1][2]
            return self.ctx.broadcast_names.get(bid, inp[1][1])
        return "message1"

    # --- Control statements ---
    def stmt_control_wait(self, b, i):
        secs = self.input_expr(b, "DURATION", "1")
        self.ctx.uses_coroutines = True
        return [f"{self._pad(i)}yield return new WaitForSeconds((float)({secs}));"]

    def stmt_control_repeat(self, b, i):
        times = self.input_expr(b, "TIMES", "10")
        pad = self._pad(i)
        lines = [f"{pad}for (int _i = 0; _i < (int)({times}); _i++) {{"]
        lines.extend(self.substack(b, "SUBSTACK", i + 1))
        lines.append(f"{pad}}}")
        return lines

    def stmt_control_forever(self, b, i):
        pad = self._pad(i)
        self.ctx.uses_coroutines = True
        lines = [f"{pad}while (true) {{"]
        lines.extend(self.substack(b, "SUBSTACK", i + 1))
        lines.append(f"{self._pad(i+1)}yield return null;")
        lines.append(f"{pad}}}")
        return lines

    def stmt_control_if(self, b, i):
        cond = self.input_expr(b, "CONDITION", "false")
        pad = self._pad(i)
        lines = [f"{pad}if ({cond}) {{"]
        lines.extend(self.substack(b, "SUBSTACK", i + 1))
        lines.append(f"{pad}}}")
        return lines

    def stmt_control_if_else(self, b, i):
        cond = self.input_expr(b, "CONDITION", "false")
        pad = self._pad(i)
        lines = [f"{pad}if ({cond}) {{"]
        lines.extend(self.substack(b, "SUBSTACK", i + 1))
        lines.append(f"{pad}}} else {{")
        lines.extend(self.substack(b, "SUBSTACK2", i + 1))
        lines.append(f"{pad}}}")
        return lines

    def stmt_control_wait_until(self, b, i):
        cond = self.input_expr(b, "CONDITION", "false")
        self.ctx.uses_coroutines = True
        return [f"{self._pad(i)}yield return new WaitUntil(() => {cond});"]

    def stmt_control_repeat_until(self, b, i):
        cond = self.input_expr(b, "CONDITION", "false")
        pad = self._pad(i)
        self.ctx.uses_coroutines = True
        lines = [f"{pad}while (!({cond})) {{"]
        lines.extend(self.substack(b, "SUBSTACK", i + 1))
        lines.append(f"{self._pad(i+1)}yield return null;")
        lines.append(f"{pad}}}")
        return lines

    def stmt_control_stop(self, b, i):
        opt = get_field(b, "STOP_OPTION", "all")
        pad = self._pad(i)
        if opt == "all":
            return [f"{pad}StopAll(); yield break;"]
        if opt == "this script":
            return [f"{pad}yield break;"]
        return [f"{pad}// stop: {opt} (not implemented)"]

    def stmt_control_create_clone_of(self, b, i):
        opt = get_field(b, "CLONE_OPTION", "_myself_")
        target = "gameObject" if opt == "_myself_" else f'FindSprite({csharp_string_literal(opt)}).gameObject'
        return [f"{self._pad(i)}CreateCloneOf({target});"]

    def stmt_control_delete_this_clone(self, b, i):
        self.ctx.uses_coroutines = True
        return [f"{self._pad(i)}Destroy(gameObject); yield break;"]

    def stmt_control_start_as_clone(self, b, i):
        return None  # hat block, handled elsewhere

    # --- Sensing statements ---
    def stmt_sensing_askandwait(self, b, i):
        q = self.input_expr(b, "QUESTION", '""')
        self.ctx.uses_coroutines = True
        return [f"{self._pad(i)}yield return StartCoroutine(AskAndWait({q}.ToString()));"]

    def stmt_sensing_setdragmode(self, b, i):
        opt = get_field(b, "DRAG_MODE", "draggable")
        return [f"{self._pad(i)}draggable = {str(opt == 'draggable').lower()};"]

    def stmt_sensing_resettimer(self, b, i):
        return [f"{self._pad(i)}scratchTimer = 0f;"]

    # --- Variables / Lists statements ---
    def stmt_data_setvariableto(self, b, i):
        vid = self._var_id_from_field(b)
        name = self.ctx.var_names.get(vid, sanitize_ident(get_field(b, "VARIABLE", "myVar")))
        v = self.input_expr(b, "VALUE", '""')
        return [f"{self._pad(i)}{name} = {v};"]

    def stmt_data_changevariableby(self, b, i):
        vid = self._var_id_from_field(b)
        name = self.ctx.var_names.get(vid, sanitize_ident(get_field(b, "VARIABLE", "myVar")))
        v = self.input_expr(b, "VALUE", "1")
        return [f"{self._pad(i)}{name} = System.Convert.ToDouble({name}) + ({v});"]

    def stmt_data_showvariable(self, b, i):
        return [f"{self._pad(i)}// show variable watcher (UI not generated)"]

    def stmt_data_hidevariable(self, b, i):
        return [f"{self._pad(i)}// hide variable watcher (UI not generated)"]

    def stmt_data_addtolist(self, b, i):
        lid = self._list_id_from_field(b)
        name = self.ctx.list_names.get(lid, "myList")
        item = self.input_expr(b, "ITEM", '""')
        return [f"{self._pad(i)}{name}.Add({item});"]

    def stmt_data_deleteoflist(self, b, i):
        lid = self._list_id_from_field(b)
        name = self.ctx.list_names.get(lid, "myList")
        idx = self.input_expr(b, "INDEX", "1")
        return [f"{self._pad(i)}if ((int)({idx}) - 1 >= 0 && (int)({idx}) - 1 < {name}.Count) {name}.RemoveAt((int)({idx}) - 1);"]

    def stmt_data_deletealloflist(self, b, i):
        lid = self._list_id_from_field(b)
        name = self.ctx.list_names.get(lid, "myList")
        return [f"{self._pad(i)}{name}.Clear();"]

    def stmt_data_insertatlist(self, b, i):
        lid = self._list_id_from_field(b)
        name = self.ctx.list_names.get(lid, "myList")
        idx = self.input_expr(b, "INDEX", "1")
        item = self.input_expr(b, "ITEM", '""')
        return [f"{self._pad(i)}{name}.Insert((int)({idx}) - 1, {item});"]

    def stmt_data_replaceitemoflist(self, b, i):
        lid = self._list_id_from_field(b)
        name = self.ctx.list_names.get(lid, "myList")
        idx = self.input_expr(b, "INDEX", "1")
        item = self.input_expr(b, "ITEM", '""')
        return [f"{self._pad(i)}{name}[(int)({idx}) - 1] = {item};"]

    def stmt_data_showlist(self, b, i):
        return [f"{self._pad(i)}// show list watcher (UI not generated)"]

    def stmt_data_hidelist(self, b, i):
        return [f"{self._pad(i)}// hide list watcher (UI not generated)"]

    # --- Custom block call ---
    def stmt_procedures_call(self, b, i):
        mutation = b.get("mutation", {})
        proccode = mutation.get("proccode", "")
        import json as _json
        try:
            arg_ids = _json.loads(mutation.get("argumentids", "[]"))
        except Exception:
            arg_ids = []
        args = []
        for aid in arg_ids:
            bid = get_input_block_id(b, aid)
            if bid:
                args.append(self.expr(bid))
            else:
                args.append(self.input_expr(b, aid, '""'))

        special = classify_special_custom_block(proccode)
        pad = self._pad(i)
        if special == "objecttype":
            return [f"{pad}// Object type marker — read by the Unity project generator (UI/Camera/GameObject), not runtime code."]
        if special == "move3d" and len(args) >= 3:
            x, y, z = args[0], args[1], args[2]
            return [f"{pad}transform.position = new Vector3((float)({x}), (float)({y}), (float)({z}));"]
        if special == "rotate":
            if len(args) >= 3:
                rx, ry, rz = args[0], args[1], args[2]
                return [f"{pad}transform.eulerAngles = new Vector3((float)({rx}), (float)({ry}), (float)({rz}));"]
            if len(args) == 1:
                return [f"{pad}transform.Rotate(Vector3.up, (float)({args[0]}));"]
        if special == "scale":
            if len(args) >= 3:
                sx, sy, sz = args[0], args[1], args[2]
                return [f"{pad}transform.localScale = new Vector3((float)({sx}), (float)({sy}), (float)({sz}));"]
            if len(args) == 1:
                return [f"{pad}transform.localScale = Vector3.one * (float)({args[0]});"]

        info = self.ctx.custom_blocks.get(proccode)
        if not info:
            return [f"{pad}// TODO: call to unknown custom block '{proccode}'"]
        method_name, argnames = info
        self.ctx.uses_coroutines = True
        return [f"{pad}yield return StartCoroutine({method_name}({', '.join(args)}));"]


def classify_special_custom_block(proccode: str):
    """Recognize custom ("My Block") blocks that stand in for real Unity
    transform ops Scratch has no native block for, so calls to them get
    inlined directly instead of going through a generated stub method:
      - "move x: _ y: _ z: _"  -> absolute transform.position set
      - anything with "rotate"/"rotation" -> Euler angle set (3 args) or
        a turn-around-up-axis (1 arg)
      - anything with "scale"                -> localScale set (3 args) or
        uniform scale (1 arg)
      - "Object type: _"                     -> project-generator metadata
        only (UI / Camera / GameObject); emits no runtime code at all.
    Matching is on the block's label words only (number/string/boolean
    input placeholders stripped), so parameter names don't matter.
    """
    base = re.sub(r"%[snb]", "", proccode).lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", base) if t]
    if "move" in tokens and "x" in tokens and "y" in tokens and "z" in tokens:
        return "move3d"
    if "rotate" in tokens or "rotation" in tokens:
        return "rotate"
    if "scale" in tokens:
        return "scale"
    if "object" in tokens and "type" in tokens:
        return "objecttype"
    return None


def detect_object_type(sprite) -> str:
    """Scan a sprite's blocks for a call to the "Object type: ()" custom
    block and return its declared literal value, normalized to one of
    "ui", "camera", or "gameobject" (the default when not declared, the
    argument isn't a literal, or the block isn't present at all)."""
    import json as _json
    for b in sprite.blocks.values():
        if not isinstance(b, dict) or b.get("opcode") != "procedures_call":
            continue
        mutation = b.get("mutation", {})
        proccode = mutation.get("proccode", "")
        if classify_special_custom_block(proccode) != "objecttype":
            continue
        try:
            arg_ids = _json.loads(mutation.get("argumentids", "[]"))
        except Exception:
            arg_ids = []
        if not arg_ids:
            continue
        value = str(get_input_primitive(b, arg_ids[0], "GameObject")).strip().lower()
        if value in ("ui", "canvas", "gui"):
            return "ui"
        if value in ("camera", "cam"):
            return "camera"
        return "gameobject"
    return "gameobject"


def scratch_key_to_unity(key: str) -> str:
    mapping = {
        "space": "Space", "up arrow": "UpArrow", "down arrow": "DownArrow",
        "left arrow": "LeftArrow", "right arrow": "RightArrow",
        "any": "AnyKey", "enter": "Return",
    }
    if key in mapping:
        return mapping[key]
    if len(key) == 1 and key.isalpha():
        return key.upper()
    if len(key) == 1 and key.isdigit():
        return f"Alpha{key}"
    return "None"
