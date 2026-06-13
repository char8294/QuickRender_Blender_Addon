bl_info = {
    "name": "Quick Render",
    "author": "KENJI",
    "version": (2, 2, 0),
    "blender": (3, 0, 0),
    "location": "3D View > N-panel > Quick Render",
    "description": "Render เฉพาะ Viewport Visible หรือ Selected Objects, สลับ Engine ชั่วคราว, เลือกกล้องหรือ Viewport พร้อมคืนค่าทุกอย่างหลังเรนเดอร์",
    "category": "Render",
}

import bpy
import os
import re
from datetime import datetime
from bpy.props import StringProperty, EnumProperty, IntProperty, BoolProperty, PointerProperty
from bpy.types import Panel, Operator, PropertyGroup
from mathutils import Vector

# -------------------- Helpers --------------------
RENDERABLE_TYPES = {'MESH','CURVE','SURFACE','META','FONT','VOLUME','GPENCIL','POINTCLOUD','CURVES'}

def get_view3d_area_region(context):
    area = context.area if context.area and context.area.type == 'VIEW_3D' else None
    if not area and context.window:
        for a in context.window.screen.areas:
            if a.type == 'VIEW_3D':
                area = a; break
    if not area: return None, None, None, None
    region = next((r for r in area.regions if r.type == 'WINDOW'), None)
    space  = next((s for s in area.spaces if s.type == 'VIEW_3D'), None)
    rv3d   = space.region_3d if space else None
    return area, region, space, rv3d

def obj_renderable(obj):
    return obj.type in RENDERABLE_TYPES

def get_viewport_visible_objects(context, scene, space, rv3d):
    """หาวัตถุที่มองเห็นจริง ๆ ใน Viewport ปัจจุบัน
    ใช้ Blender API visible_get() ตรวจสอบ visibility ทุกระดับ:
    - Eye icon (hide_get)
    - Monitor icon (hide_viewport)
    - Collection visibility
    - View Layer visibility
    - Local View (numpad /)
    แล้วเสริมด้วย frustum check ว่าอยู่ในมุมมองหรือไม่
    """
    visible = []
    view_layer = context.view_layer

    for obj in scene.objects:
        if not obj_renderable(obj):
            continue

        # ใช้ Blender API ตรวจ visibility ทุกระดับ
        try:
            if not obj.visible_get(view_layer=view_layer, viewport=space):
                continue
        except TypeError:
            # Blender เวอร์ชันเก่าที่ visible_get ไม่รับ keyword args
            if obj.hide_get() or obj.hide_viewport:
                continue
            if space and space.local_view:
                if not obj.local_view_get(space):
                    continue

        # Frustum check: ตรวจว่า object อยู่ในมุมมอง viewport หรือไม่
        if rv3d and _is_in_frustum(obj, rv3d):
            visible.append(obj)

    return visible

def _is_in_frustum(obj, rv3d):
    """ตรวจว่า bounding box ของ object อยู่ใน view frustum หรือไม่
    ใช้ perspective_matrix แปลง world coords → NDC แล้วเช็ค range
    """
    P = rv3d.perspective_matrix

    try:
        corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    except Exception:
        corners = [obj.matrix_world.translation]

    # ถ้าไม่มี bound_box (เช่น empty) ให้ถือว่าอยู่ใน frustum
    if not corners:
        return True

    ndc_points = []
    has_behind = False
    for p in corners:
        cp = P @ Vector((p.x, p.y, p.z, 1.0))
        w = cp.w
        if w <= 0.0:
            # จุดอยู่หลังกล้อง
            has_behind = True
            ndc_points.append(None)
            continue
        ndc_points.append((cp.x / w, cp.y / w, cp.z / w))

    # ถ้ามี corner อยู่หลังกล้อง → วัตถุน่าจะมองเห็นได้ (ครอบคลุมกล้อง)
    if has_behind:
        # ถ้ามีบาง corner อยู่หน้ากล้องด้วย → แน่นอนว่ามองเห็น
        if any(p is not None for p in ndc_points):
            return True
        # ทุก corner อยู่หลังกล้อง → ไม่มองเห็น
        return False

    # ตรวจว่ามี corner ใดอยู่ใน NDC range [-1,1] ทั้ง x, y, z
    for p in ndc_points:
        if p is None:
            continue
        x, y, z = p
        if -1.0 <= x <= 1.0 and -1.0 <= y <= 1.0 and -1.0 <= z <= 1.0:
            return True

    # AABB overlap test: วัตถุใหญ่อาจมีทุก corner อยู่นอก NDC
    # แต่ body ยังคาดคร่อมอยู่ในจอ
    valid = [p for p in ndc_points if p is not None]
    if valid:
        xs = [p[0] for p in valid]
        ys = [p[1] for p in valid]
        zs = [p[2] for p in valid]
        if (min(xs) <= 1.0 and max(xs) >= -1.0 and
            min(ys) <= 1.0 and max(ys) >= -1.0 and
            min(zs) <= 1.0 and max(zs) >= -1.0):
            return True

    return False

def sanitize_name(name):
    return re.sub(r'[^A-Za-z0-9._-]+', '_', name)

def ensure_output_dir(path):
    out_dir = bpy.path.abspath(path)
    os.makedirs(out_dir, exist_ok=True)
    return out_dir

def apply_render_size_and_format(scene, props):
    r = scene.render
    r.resolution_x = props.resolution_x
    r.resolution_y = props.resolution_y
    r.resolution_percentage = int(props.resolution_scale)
    r.image_settings.file_format = props.file_format
    if props.file_format == 'PNG':
        r.image_settings.color_depth = props.color_depth
        r.image_settings.compression = 15
        return ".png"
    else:
        r.image_settings.quality = props.jpeg_quality
        return ".jpg"

def hide_all(scene, value=True):
    for o in scene.objects:
        o.hide_render = value

def restore_hide(scene, original):
    for name, h in original.items():
        o = scene.objects.get(name)
        if o: o.hide_render = h

# --- camera-from-viewport ---
def align_camera_to_viewport_without_switch(context, cam):
    area, region, space, rv3d = get_view3d_area_region(context)
    if not rv3d: return False, None
    cam.matrix_world = rv3d.view_matrix.inverted()
    if rv3d.is_perspective:
        cam.data.type = 'PERSP'
        cam.data.lens = space.lens
    else:
        cam.data.type = 'ORTHO'
    return True, rv3d

# -------------------- Poll / Update for camera pointer --------------------
def camera_poll(self, obj):
    return obj.type == 'CAMERA'

def on_camera_selected(self, context):
    """เมื่อเลือกกล้องใน dropdown → ตั้งเป็น active camera ของ scene ด้วย"""
    if self.selected_camera and self.selected_camera.type == 'CAMERA':
        context.scene.camera = self.selected_camera

# -------------------- Properties --------------------
class QVR_Props(PropertyGroup):
    engine: EnumProperty(
        name="Render Engine",
        items=[
            ('BLENDER_EEVEE', "Eevee", ""),
            ('CYCLES', "Cycles", ""),
            ('BLENDER_WORKBENCH', "Workbench", "")
        ],
        default='BLENDER_EEVEE'
    )
    use_camera_view: BoolProperty(
        name="Use Camera View",
        description="เรนเดอร์จากมุมกล้อง (ติ๊ก) หรือมุม Viewport ปัจจุบัน (ไม่ติ๊ก)",
        default=False
    )
    selected_camera: PointerProperty(
        name="Camera",
        type=bpy.types.Object,
        poll=camera_poll,
        update=on_camera_selected,
        description="เลือกกล้องที่จะใช้เรนเดอร์ (จะตั้งเป็น Active Camera ของ Scene ด้วย)"
    )
    render_mode: EnumProperty(
        name="Mode",
        description="เลือกโหมดการเรนเดอร์",
        items=[
            ('VIEWPORT', "Viewport Visible", "เรนเดอร์เฉพาะวัตถุที่มองเห็นใน Viewport"),
            ('SELECTED', "Selected Only", "เรนเดอร์เฉพาะวัตถุที่ถูกเลือก")
        ],
        default='VIEWPORT'
    )
    output_dir: StringProperty(name="Output Folder", subtype='DIR_PATH', default="//renders/")

    # --- Render & Save naming ---
    render_filename: StringProperty(name="Filename", default="render", description="ชื่อไฟล์สำหรับ Render & Save")
    render_timestamp: BoolProperty(name="Timestamp", default=True, description="เพิ่มวันที่-เวลาต่อท้ายชื่อไฟล์")

    # --- Batch naming ---
    batch_prefix: StringProperty(name="Prefix", default="", description="คำนำหน้าต่อตรงกับชื่อ Object")
    batch_suffix: StringProperty(name="Suffix", default="", description="คำต่อท้ายชื่อ Object")
    batch_timestamp: BoolProperty(name="Timestamp", default=True, description="เพิ่มวันที่-เวลาต่อท้ายชื่อไฟล์ Batch")
    file_format: EnumProperty(name="Format", items=[('PNG',"PNG",""),('JPEG',"JPEG","")], default='PNG')
    color_depth: EnumProperty(name="Depth", items=[('8',"8-bit",""),('16',"16-bit","")], default='8')
    jpeg_quality: IntProperty(name="JPEG Quality", min=1, max=100, default=95)
    resolution_x: IntProperty(name="W", min=8, max=16384, default=1920)
    resolution_y: IntProperty(name="H", min=8, max=16384, default=1080)
    resolution_scale: EnumProperty(
        name="Scale",
        description="ตัวคูณความละเอียด",
        items=[
            ('100', "x1", "ความละเอียดปกติ"),
            ('200', "x2", "2 เท่า"),
            ('300', "x3", "3 เท่า"),
            ('400', "x4", "4 เท่า"),
        ],
        default='100'
    )

    # --- Workbench settings ---
    wb_show_settings: BoolProperty(
        name="Workbench Settings",
        description="แสดง/ซ่อนการตั้งค่า Workbench",
        default=False
    )
    wb_light: EnumProperty(
        name="Lighting",
        items=[
            ('STUDIO', "Studio", ""),
            ('MATCAP', "MatCap", ""),
            ('FLAT', "Flat", ""),
        ],
        default='STUDIO'
    )
    wb_color_type: EnumProperty(
        name="Color",
        items=[
            ('MATERIAL', "Material", ""),
            ('SINGLE', "Single", ""),
            ('OBJECT', "Object", ""),
            ('RANDOM', "Random", ""),
            ('VERTEX', "Vertex", ""),
            ('TEXTURE', "Texture", ""),
        ],
        default='MATERIAL'
    )
    wb_film_transparent: BoolProperty(
        name="Film Transparent",
        description="พื้นหลังโปร่งใส",
        default=False
    )
    wb_view_transform: EnumProperty(
        name="View Transform",
        items=[
            ('Standard', "Standard", ""),
            ('Filmic', "Filmic", ""),
            ('AgX', "AgX", ""),
            ('Raw', "Raw", ""),
            ('False Color', "False Color", ""),
        ],
        default='Standard'
    )

# -------------------- Core Render Routine --------------------
def render_core(context, *, save_file: bool, batch_objects=None):
    scene = context.scene
    props = scene.qvr_props

    # --- กล้อง ---
    cam_created = False
    original_scene_camera = scene.camera
    switched_camera = False

    if props.use_camera_view:
        if props.selected_camera and props.selected_camera.type == 'CAMERA':
            scene.camera = props.selected_camera
            switched_camera = True
        if not scene.camera:
            raise RuntimeError("ไม่มีกล้องในซีน กรุณาเพิ่มกล้องหรือปิด Use Camera View")
    else:
        if not scene.camera:
            cam_data = bpy.data.cameras.new("QVR_TempCam")
            cam_obj = bpy.data.objects.new("QVR_TempCam", cam_data)
            scene.collection.objects.link(cam_obj)
            scene.camera = cam_obj
            cam_created = True

    cam = scene.camera

    # เก็บสถานะเดิม
    original_hide   = {o.name: o.hide_render for o in scene.objects}
    original_engine = scene.render.engine

    r = scene.render
    orig_res_x   = r.resolution_x
    orig_res_y   = r.resolution_y
    orig_res_pct = r.resolution_percentage

    cam_matrix_before = cam.matrix_world.copy()
    cam_type_before   = cam.data.type
    lens_before       = getattr(cam.data, "lens", None)
    ortho_before      = getattr(cam.data, "ortho_scale", None)

    # เก็บค่า Workbench เดิม
    orig_film_transparent = r.film_transparent
    orig_view_transform   = scene.view_settings.view_transform

    # เลือกมุมกล้อง
    area, region, space, rv3d = get_view3d_area_region(context)
    if not rv3d:
        raise RuntimeError("ไม่พบ 3D Viewport")

    if not props.use_camera_view:
        ok, rv3d = align_camera_to_viewport_without_switch(context, cam)
        if not ok:
            raise RuntimeError("ตั้งกล้องจากมุมมอง Viewport ไม่สำเร็จ")

    # ชุดที่จะเปิด render
    if batch_objects is not None:
        visible = [o for o in batch_objects if obj_renderable(o)]
    elif props.render_mode == 'SELECTED':
        visible = [o for o in scene.objects if o.select_get() and obj_renderable(o)]
        if not visible:
            raise RuntimeError("ไม่ได้เลือกวัตถุที่สามารถเรนเดอร์ได้")
    else:
        visible = get_viewport_visible_objects(context, scene, space, rv3d)
        if not visible:
            raise RuntimeError("Viewport ไม่มีวัตถุที่มองเห็น")

    # ตั้งขนาด/ฟอร์แมต
    ext = apply_render_size_and_format(scene, props)

    try:
        scene.render.engine = props.engine

        # ตั้งค่า Workbench ถ้าเลือก Workbench
        if props.engine == 'BLENDER_WORKBENCH':
            scene.display.shading.light = props.wb_light
            scene.display.shading.color_type = props.wb_color_type
            r.film_transparent = props.wb_film_transparent
            scene.view_settings.view_transform = props.wb_view_transform

        hide_all(scene, True)
        for o in visible: o.hide_render = False

        if save_file:
            bpy.ops.render.render(write_still=True)
        else:
            bpy.ops.render.render()
            try: bpy.ops.render.view_show('INVOKE_DEFAULT')
            except Exception: pass

    finally:
        restore_hide(scene, original_hide)
        scene.render.engine = original_engine
        cam.matrix_world = cam_matrix_before
        cam.data.type = cam_type_before
        if lens_before is not None:  cam.data.lens = lens_before
        if ortho_before is not None: cam.data.ortho_scale = ortho_before
        # คืนค่า Workbench settings
        r.film_transparent = orig_film_transparent
        scene.view_settings.view_transform = orig_view_transform
        if cam_created:
            tmp = scene.camera
            scene.camera = original_scene_camera
            try: scene.collection.objects.unlink(tmp)
            except Exception: pass
            try: bpy.data.objects.remove(tmp, do_unlink=True)
            except Exception: pass
        elif switched_camera:
            scene.camera = original_scene_camera
        r.resolution_x = orig_res_x
        r.resolution_y = orig_res_y
        r.resolution_percentage = orig_res_pct

    return ext

# -------------------- Operators --------------------
class QVR_OT_render_save(Operator):
    bl_idname = "qvr.render_save"
    bl_label = "Render & Save"
    bl_description = "เรนเดอร์ตามโหมดที่เลือกแล้วบันทึกไฟล์"

    def execute(self, context):
        scene = context.scene
        props = scene.qvr_props

        out_dir = ensure_output_dir(props.output_dir)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S") if props.render_timestamp else ""
        base = props.render_filename + (f"_{ts}" if ts else "")
        ext = apply_render_size_and_format(scene, props)
        scene.render.filepath = os.path.join(out_dir, base + ext)

        try:
            render_core(context, save_file=True)
        except Exception as e:
            self.report({'ERROR'}, str(e)); return {'CANCELLED'}

        self.report({'INFO'}, f"บันทึก: {scene.render.filepath}")
        return {'FINISHED'}

class QVR_OT_render_preview(Operator):
    bl_idname = "qvr.render_preview"
    bl_label = "Preview"
    bl_description = "เรนเดอร์ตามโหมดที่เลือก (ดูตัวอย่าง ไม่บันทึกไฟล์)"

    def execute(self, context):
        try:
            render_core(context, save_file=False)
        except Exception as e:
            self.report({'ERROR'}, str(e)); return {'CANCELLED'}

        self.report({'INFO'}, "พรีวิวใน Render Result (ไม่เซฟไฟล์)")
        return {'FINISHED'}

class QVR_OT_batch_render_selected(Operator):
    bl_idname = "qvr.batch_render_selected"
    bl_label = "Batch Render Selected"
    bl_description = "เรนเดอร์วัตถุที่เลือกทีละชิ้น บันทึกแยกไฟล์"

    def execute(self, context):
        scene = context.scene
        props = scene.qvr_props

        sel = [o for o in scene.objects if o.select_get() and obj_renderable(o)]
        if not sel:
            self.report({'ERROR'}, "ไม่ได้เลือกวัตถุที่สามารถเรนเดอร์ได้"); return {'CANCELLED'}

        out_dir = ensure_output_dir(props.output_dir)
        ext = apply_render_size_and_format(scene, props)
        ts  = datetime.now().strftime("%Y%m%d_%H%M%S") if props.batch_timestamp else ""

        rendered = 0
        for obj in sel:
            safe = sanitize_name(obj.name)
            # สร้างชื่อ: prefix + ObjName + suffix + timestamp
            name = props.batch_prefix + safe
            if props.batch_suffix:
                name += "_" + props.batch_suffix
            if ts:
                name += "_" + ts
            scene.render.filepath = os.path.join(out_dir, name + ext)
            try:
                render_core(context, save_file=True, batch_objects=[obj])
                rendered += 1
            except Exception as e:
                self.report({'WARNING'}, f"ข้าม {obj.name}: {e}")

        if rendered:
            self.report({'INFO'}, f"เสร็จ {rendered} ไฟล์")
            return {'FINISHED'}
        else:
            self.report({'WARNING'}, "ไม่มีไฟล์ถูกเรนเดอร์")
            return {'CANCELLED'}

# -------------------- UI --------------------
class QVR_PT_panel(Panel):
    bl_label = "Quick Render"
    bl_idname = "QVR_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Quick Render'

    def draw(self, context):
        props = context.scene.qvr_props
        layout = self.layout

        # ═══════════ Engine ═══════════
        box = layout.box()
        row = box.row()
        row.label(text="Engine", icon='PREFERENCES')
        row.separator(factor=1.0)
        row.prop(props, "engine", text="")

        # ── Workbench Settings (collapsible) ──
        if props.engine == 'BLENDER_WORKBENCH':
            row = box.row()
            row.prop(props, "wb_show_settings",
                     icon='TRIA_DOWN' if props.wb_show_settings else 'TRIA_RIGHT',
                     text="Workbench Settings", emboss=False)
            if props.wb_show_settings:
                col = box.column(align=True)
                col.prop(props, "wb_light")
                col.prop(props, "wb_color_type")
                col.separator()
                col.prop(props, "wb_film_transparent")
                col.prop(props, "wb_view_transform")

        # ═══════════ Camera ═══════════
        box = layout.box()
        box.prop(props, "use_camera_view", icon='CAMERA_DATA')
        if props.use_camera_view:
            row = box.row()
            row.prop(props, "selected_camera", text="", icon='OUTLINER_OB_CAMERA')

        # ═══════════ Resolution ═══════════
        box = layout.box()
        box.label(text="Resolution", icon='FULLSCREEN_ENTER')
        row = box.row()
        split = row.split(factor=0.35)
        split.prop(props, "resolution_x", text="W")
        split2 = split.split(factor=0.55)
        split2.prop(props, "resolution_y", text="H")
        split2.prop(props, "resolution_scale", text="")

        # ═══════════ Output ═══════════
        box = layout.box()
        box.label(text="Output", icon='FILE_FOLDER')
        box.prop(props, "output_dir", text="")

        row = box.row(align=True)
        row.prop(props, "file_format", text="")
        if props.file_format == 'PNG':
            row.prop(props, "color_depth", text="")
        else:
            row.prop(props, "jpeg_quality")

        # ═══════════ Render ═══════════
        ext = ".png" if props.file_format == 'PNG' else ".jpg"

        box = layout.box()
        box.label(text="Render", icon='RENDER_STILL')
        box.prop(props, "render_mode", text="")

        box.prop(props, "render_filename")
        box.prop(props, "render_timestamp")

        # Preview ชื่อไฟล์
        ts_r = "_20260101_120000" if props.render_timestamp else ""
        row = box.row()
        row.alignment = 'LEFT'
        row.scale_y = 0.7
        row.label(text=f"  → {props.render_filename}{ts_r}{ext}", icon='INFO')

        row = box.row(align=True)
        row.scale_y = 1.5
        row.operator("qvr.render_save", text="Render & Save", icon='OUTPUT')
        row.operator("qvr.render_preview", text="Preview", icon='RESTRICT_VIEW_OFF')

        # ── Batch ──
        box = layout.box()
        box.label(text="Batch Render", icon='RENDER_ANIMATION')

        box.prop(props, "batch_prefix")
        box.prop(props, "batch_suffix")
        box.prop(props, "batch_timestamp")

        # Preview ชื่อไฟล์
        ts_b = "_20260101_120000" if props.batch_timestamp else ""
        sfx  = f"_{props.batch_suffix}" if props.batch_suffix else ""
        row = box.row()
        row.alignment = 'LEFT'
        row.scale_y = 0.7
        row.label(text=f"  → {props.batch_prefix}[ObjName]{sfx}{ts_b}{ext}", icon='INFO')

        col = box.column(align=True)
        col.scale_y = 1.2
        col.operator("qvr.batch_render_selected", text="Batch Render Selected", icon='DOCUMENTS')

# -------------------- Register --------------------
classes = (
    QVR_Props,
    QVR_OT_render_save,
    QVR_OT_render_preview,
    QVR_OT_batch_render_selected,
    QVR_PT_panel,
)

def register():
    for c in classes: bpy.utils.register_class(c)
    bpy.types.Scene.qvr_props = bpy.props.PointerProperty(type=QVR_Props)

def unregister():
    del bpy.types.Scene.qvr_props
    for c in reversed(classes): bpy.utils.unregister_class(c)

if __name__ == "__main__":
    register()
