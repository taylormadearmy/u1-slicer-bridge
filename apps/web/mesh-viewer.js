/**
 * Pre-slice placement viewer for M33/M36 shared workflow.
 *
 * v2:
 * - Renders actual object meshes when /geometry is available (proxy fallback otherwise)
 * - Supports mode-based left-drag interactions on objects (move / rotate)
 * - Keeps orbit/pan/zoom navigation (left-drag empty space, right-drag pan, wheel zoom)
 */

(function () {
    class MeshPlacementViewer {
        constructor(canvas, options = {}) {
            this.canvas = canvas;
            this.options = options;
            this.renderer = null;
            this.scene = null;
            this.camera = null;
            this.rootGroup = null;
            this.plateGroup = null;
            this.objectsGroup = null;
            this.raycaster = null;
            this.pointer = new THREE.Vector2();
            this.resizeObserver = null;
            this.bedPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);

            this.layout = null;
            this.geometryData = null;
            this.getPose = null;
            this.selectedBuildItemIndex = null;
            this.interactionMode = 'move';
            this.primeTower = null;

            this.objectMeshes = new Map(); // build_item_index -> group
            this.boundsByIndex = new Map();
            this.geometryByIndex = new Map();
            this.primeTowerGroup = null;

            this.target = new THREE.Vector3(135, 0, 135);
            // Match gcode-preview's default camera vector (approximately
            // [-100, +400, +450] from bed center) so pre-slice and G-code
            // previews compare in the same screen orientation.
            this.orbit = { radius: 610, theta: 1.79, phi: 0.72 };
            this.dragState = null;

            this._raf = null;
            this._animating = false;
            this._boundAnimate = () => this._animate();
        }

        init() {
            if (!this.canvas || this.renderer) return;
            // Don't retry after a failed init — avoids a context-creation storm
            // when setLayout/setPrimeTower both call init() on every update.
            if (this._initFailed) return;

            try {
            this.renderer = new THREE.WebGLRenderer({
                canvas: this.canvas,
                antialias: true,
                alpha: false,
            });
            } catch (e) {
                console.warn('[placement-viewer] WebGL context creation failed:', e.message);
                this._initFailed = true;
                this.renderer = null;
                return;
            }
            this.renderer.setPixelRatio(window.devicePixelRatio || 1);
            this.renderer.setClearColor(0xf8fafc, 1);

            this.scene = new THREE.Scene();
            this.camera = new THREE.PerspectiveCamera(35, 1, 0.1, 5000);
            this.raycaster = new THREE.Raycaster();

            this.rootGroup = new THREE.Group();
            this.scene.add(this.rootGroup);

            this.plateGroup = new THREE.Group();
            this.objectsGroup = new THREE.Group();
            this.rootGroup.add(this.plateGroup);
            this.rootGroup.add(this.objectsGroup);

            this._addLights();
            this._wireEvents();
            this._resize();
            this._updateCamera();
            this._render();
            this._startLoop();
        }

        destroy() {
            this._stopLoop();
            if (this.resizeObserver) {
                this.resizeObserver.disconnect();
                this.resizeObserver = null;
            }
            if (this.canvas) {
                this.canvas.removeEventListener('pointerdown', this._onPointerDown);
                this.canvas.removeEventListener('pointermove', this._onPointerMove);
                this.canvas.removeEventListener('pointerup', this._onPointerUp);
                this.canvas.removeEventListener('pointerleave', this._onPointerUp);
                this.canvas.removeEventListener('wheel', this._onWheel);
                this.canvas.removeEventListener('contextmenu', this._onContextMenu);
            }
            if (this.renderer) {
                // forceContextLoss() is required to actually release the WebGL
                // context slot in Chrome. dispose() alone doesn't free it, so
                // repeated file opens exhaust the browser's context limit (~8-16).
                this.renderer.forceContextLoss();
                this.renderer.dispose();
            }
            this.objectMeshes.clear();
            this.boundsByIndex.clear();
            this.geometryByIndex.clear();
            this.renderer = null;
            this.scene = null;
            this.camera = null;
            this._initFailed = false; // Reset so next init attempt can succeed
        }

        setLayout(layout, getPoseFn, geometryData = null) {
            const sameLayoutRef = this.layout === (layout || null);
            const sameGeomRef = this.geometryData === (geometryData || null);
            this.layout = layout || null;
            this.geometryData = geometryData || null;
            this.getPose = typeof getPoseFn === 'function' ? getPoseFn : null;

            this.geometryByIndex.clear();
            for (const obj of (this.geometryData?.objects || [])) {
                const idx = Number(obj?.build_item_index || 0);
                if (idx > 0) this.geometryByIndex.set(idx, obj);
            }

            if (!this.renderer) this.init();

            if (this._initFailed) { this._render2D(); return; }

            if (!this.layout) {
                this._rebuildScene();
                return;
            }

            if (!sameLayoutRef || !sameGeomRef) {
                this._rebuildScene();
            } else {
                this.refreshPoses();
            }
        }

        setSelected(buildItemIndex) {
            const idx = Number(buildItemIndex || 0) || null;
            this.selectedBuildItemIndex = idx;
            if (this._initFailed) { this._render2D(); return; }
            this._refreshObjectStyles();
        }

        getDebugObjectRenderState(buildItemIndex) {
            const idx = Number(buildItemIndex || 0);
            if (!idx) return null;
            const group = this.objectMeshes.get(idx);
            if (!group) return null;
            const base = this.boundsByIndex.get(idx) || { size: [0, 0, 0] };
            const meshSolid = group.userData?.meshSolid || null;
            const proxy = group.userData?.proxy || null;
            const activeScale = meshSolid?.scale || proxy?.scale || { x: 1, y: 1, z: 1 };
            const sx = Number(activeScale.x || 1);
            const sy = Number(activeScale.y || 1);
            const sz = Number(activeScale.z || 1);
            const size = Array.isArray(base.size) ? base.size : [0, 0, 0];
            return {
                build_item_index: idx,
                meshScale: { x: sx, y: sy, z: sz },
                baseSize3mf: {
                    x: Number(size[0] || 0),
                    y: Number(size[1] || 0),
                    z: Number(size[2] || 0),
                },
                // Viewer bed plane is X/Z (Three) == X/Y (3MF) after axis remap.
                bedFootprintEstimate: {
                    x: Number(size[0] || 0) * sx,
                    y: Number(size[1] || 0) * sz,
                },
            };
        }

        setPrimeTower(primeTower) {
            const prev = this.primeTower || null;
            const next = primeTower || null;
            const same =
                (!!prev === !!next) &&
                (!prev || !next || (
                    Number(prev.x || 0) === Number(next.x || 0) &&
                    Number(prev.y || 0) === Number(next.y || 0) &&
                    Number(prev.width || 0) === Number(next.width || 0) &&
                    Number(prev.brim_width || 0) === Number(next.brim_width || 0) &&
                    !!prev.explicit_position === !!next.explicit_position
                ));
            this.primeTower = next;
            if (!this.renderer) this.init();
            if (this._initFailed) { this._render2D(); return; }
            if (same) {
                this._refreshPrimeTowerPose();
                this._render();
                return;
            }
            this._rebuildScene();
        }

        setInteractionMode(mode) {
            this.interactionMode = (mode === 'rotate') ? 'rotate' : 'move';
        }

        _bedDepth() {
            const vol = this.layout?.build_volume || { y: 270 };
            return Math.max(1, Number(vol.y || 270));
        }

        _bedYToWorldZ(y) {
            return this._bedDepth() - Number(y || 0);
        }

        _worldZToBedY(z) {
            return this._bedDepth() - Number(z || 0);
        }

        _bedLocalYToLocalZ(y) {
            return -Number(y || 0);
        }

        refreshPoses() {
            if (this._initFailed) { this._render2D(); return; }
            if (!this.objectsGroup || !this.layout || !this.getPose) return;
            for (const obj of (this.layout.objects || [])) {
                const idx = Number(obj.build_item_index || 0);
                const group = this.objectMeshes.get(idx);
                if (!group) continue;

                const pose = this.getPose(obj) || { x: 0, y: 0, z: 0, rotate_z_deg: 0 };
                const base = this.boundsByIndex.get(idx) || { centerLocal: [0, 0, 0], size: [20, 20, 10] };
                const scale = this._getObjectLocalScale(obj);
                group.position.set(Number(pose.x || 0), Number(pose.z || 0), this._bedYToWorldZ(pose.y));
                group.rotation.set(0, THREE.MathUtils.degToRad(Number(pose.rotate_z_deg || 0)), 0);

                // For proxy-only rendering, offset the box so it rotates around approximate center.
                // Proxy uses layout bounds + transform scale since it has no actual geometry.
                if (group.userData.proxy && !group.userData.meshSolid) {
                    group.userData.proxy.scale.set(scale.x, scale.y, scale.z);
                    group.userData.proxy.position.set(
                        Number(base.centerLocal[0] || 0),
                        Number(base.size[2] || 10) / 2,
                        this._bedLocalYToLocalZ(base.centerLocal[1] || 0),
                    );
                }
                // Actual mesh geometry has the build item rotation+scale pre-applied
                // by the backend geometry API.  Do NOT re-apply transform scale here
                // or the mesh will be double-scaled.
                if (group.userData.labelSprite) {
                    group.userData.labelSprite.position.set(0, Math.max(8, (Number(base.size[2] || 10) * scale.y) + 8), 0);
                }
            }
            this._refreshPrimeTowerPose();
            this._refreshObjectStyles();
        }

        _getObjectLocalScale(obj) {
            const t = Array.isArray(obj?.transform_3x4) ? obj.transform_3x4 : null;
            if (!t || t.length < 9) return { x: 1, y: 1, z: 1 };
            const len = (a, b, c) => {
                const v = Math.hypot(Number(a || 0), Number(b || 0), Number(c || 0));
                return Number.isFinite(v) && v > 1e-6 ? v : 1;
            };
            // 3MF transform columns represent local X/Y/Z axes in world space.
            // Viewer maps 3MF -> Three as (x, y, z) -> (x, z, y), so swap Y/Z scales.
            const sx3mf = len(t[0], t[3], t[6]);
            const sy3mf = len(t[1], t[4], t[7]);
            const sz3mf = len(t[2], t[5], t[8]);
            return {
                x: sx3mf,
                y: sz3mf, // Three Y is 3MF Z
                z: sy3mf, // Three Z is 3MF Y
            };
        }

        _addLights() {
            this.scene.add(new THREE.AmbientLight(0xffffff, 0.65));
            const dirA = new THREE.DirectionalLight(0xffffff, 0.95);
            dirA.position.set(-220, 260, 180);
            this.scene.add(dirA);
            const dirB = new THREE.DirectionalLight(0xffffff, 0.45);
            dirB.position.set(180, 140, -220);
            this.scene.add(dirB);
        }

        _wireEvents() {
            this._onContextMenu = (e) => e.preventDefault();

            this._onPointerDown = (e) => {
                if (!this.renderer || !this.camera) return;
                this.canvas.setPointerCapture?.(e.pointerId);

                const hit = this._pickObjectHit(e);
                if (hit?.buildItemIndex && this.options.onSelect) {
                    this.options.onSelect(hit.buildItemIndex);
                }

                const leftButton = (e.button === 0);
                const mode = (typeof this.options.getInteractionMode === 'function')
                    ? (this.options.getInteractionMode() || this.interactionMode)
                    : this.interactionMode;
                const canEditObjects = (typeof this.options.canEditObjects === 'function')
                    ? !!this.options.canEditObjects()
                    : true;

                if (leftButton && hit?.buildItemIndex && canEditObjects && (mode === 'move' || mode === 'rotate')) {
                    const planePoint = this._intersectBedPlane(e);
                    const group = this.objectMeshes.get(Number(hit.buildItemIndex));
                    if (planePoint && group) {
                        const currentObj = (this.layout?.objects || []).find(
                            o => Number(o.build_item_index || 0) === Number(hit.buildItemIndex)
                        );
                        const pose = this.getPose?.(currentObj) || {
                            x: group.position.x,
                            y: this._worldZToBedY(group.position.z),
                            z: group.position.y,
                            rotate_z_deg: THREE.MathUtils.radToDeg(group.rotation.y || 0),
                        };
                        const center = new THREE.Vector3(group.position.x, 0, group.position.z);
                        const startAngle = Math.atan2(center.z - planePoint.z, planePoint.x - center.x);
                        this.dragState = {
                            kind: mode,
                            pointerId: e.pointerId,
                            x: e.clientX,
                            y: e.clientY,
                            moved: false,
                            buildItemIndex: Number(hit.buildItemIndex),
                            startPlanePoint: planePoint.clone(),
                            startPose: {
                                x: Number(pose.x || 0),
                                y: Number(pose.y || 0),
                                rotate_z_deg: Number(pose.rotate_z_deg || 0),
                            },
                            startAngle,
                        };
                        e.preventDefault();
                        return;
                    }
                }

                if (leftButton && hit?.kind === 'primeTower' && mode === 'move') {
                    const planePoint = this._intersectBedPlane(e);
                    if (planePoint && this.primeTowerGroup) {
                        this.dragState = {
                            kind: 'primeTowerMove',
                            pointerId: e.pointerId,
                            x: e.clientX,
                            y: e.clientY,
                            moved: false,
                            startPlanePoint: planePoint.clone(),
                            startPose: {
                                x: Number(this.primeTower?.x || this.primeTowerGroup.position.x || 0),
                                y: Number(this.primeTower?.y || this.primeTowerGroup.position.z || 0),
                            },
                        };
                        e.preventDefault();
                        return;
                    }
                }

                this.dragState = {
                    kind: 'nav',
                    pointerId: e.pointerId,
                    button: e.button,
                    x: e.clientX,
                    y: e.clientY,
                    moved: false,
                };
            };

            this._onPointerMove = (e) => {
                if (!this.dragState || e.pointerId !== this.dragState.pointerId) return;
                const dx = e.clientX - this.dragState.x;
                const dy = e.clientY - this.dragState.y;
                if (Math.abs(dx) + Math.abs(dy) > 2) this.dragState.moved = true;
                this.dragState.x = e.clientX;
                this.dragState.y = e.clientY;

                if (this.dragState.kind === 'nav') {
                    if (this.dragState.button === 2 || this.dragState.button === 1) {
                        this._pan(dx, dy);
                    } else {
                        this._orbit(dx, dy);
                    }
                    this._render();
                    return;
                }

                if (this.dragState.kind === 'move') {
                    const planePoint = this._intersectBedPlane(e);
                    if (!planePoint) return;
                    const deltaX = planePoint.x - this.dragState.startPlanePoint.x;
                    const deltaY = -(planePoint.z - this.dragState.startPlanePoint.z);
                    const nextPose = {
                        x: this.dragState.startPose.x + deltaX,
                        y: this.dragState.startPose.y + deltaY,
                        rotate_z_deg: this.dragState.startPose.rotate_z_deg,
                    };
                    this._applyPoseEdit(this.dragState.buildItemIndex, nextPose);
                    return;
                }

                if (this.dragState.kind === 'rotate') {
                    const planePoint = this._intersectBedPlane(e);
                    if (!planePoint) return;
                    const group = this.objectMeshes.get(Number(this.dragState.buildItemIndex));
                    if (!group) return;
                    const center = new THREE.Vector3(group.position.x, 0, group.position.z);
                    const currentAngle = Math.atan2(center.z - planePoint.z, planePoint.x - center.x);
                    let deltaDeg = THREE.MathUtils.radToDeg(currentAngle - this.dragState.startAngle);
                    while (deltaDeg > 180) deltaDeg -= 360;
                    while (deltaDeg < -180) deltaDeg += 360;
                    let nextDeg = this.dragState.startPose.rotate_z_deg + deltaDeg;
                    if (e.shiftKey) {
                        nextDeg = Math.round(nextDeg / 15) * 15;
                    }
                    const nextPose = {
                        x: this.dragState.startPose.x,
                        y: this.dragState.startPose.y,
                        rotate_z_deg: nextDeg,
                    };
                    this._applyPoseEdit(this.dragState.buildItemIndex, nextPose);
                    return;
                }

                if (this.dragState.kind === 'primeTowerMove') {
                    const planePoint = this._intersectBedPlane(e);
                    if (!planePoint) return;
                    const deltaX = planePoint.x - this.dragState.startPlanePoint.x;
                    const deltaY = -(planePoint.z - this.dragState.startPlanePoint.z);
                    const nextPose = {
                        x: this.dragState.startPose.x + deltaX,
                        y: this.dragState.startPose.y + deltaY,
                    };
                    this._applyPrimeTowerMove(nextPose);
                }
            };

            this._onPointerUp = (e) => {
                if (this.dragState && e.pointerId === this.dragState.pointerId) {
                    this.dragState = null;
                }
            };

            this._onWheel = (e) => {
                e.preventDefault();
                const factor = e.deltaY > 0 ? 1.08 : 0.92;
                this.orbit.radius = Math.max(80, Math.min(1500, this.orbit.radius * factor));
                this._updateCamera();
                this._render();
            };

            this.canvas.addEventListener('contextmenu', this._onContextMenu);
            this.canvas.addEventListener('pointerdown', this._onPointerDown);
            this.canvas.addEventListener('pointermove', this._onPointerMove);
            this.canvas.addEventListener('pointerup', this._onPointerUp);
            this.canvas.addEventListener('pointerleave', this._onPointerUp);
            this.canvas.addEventListener('wheel', this._onWheel, { passive: false });

            this.resizeObserver = new ResizeObserver(() => {
                this._resize();
                this._render();
            });
            this.resizeObserver.observe(this.canvas.parentElement || this.canvas);
        }

        _applyPoseEdit(buildItemIndex, nextPose) {
            const group = this.objectMeshes.get(Number(buildItemIndex));
            if (group) {
                group.position.set(Number(nextPose.x || 0), group.position.y, this._bedYToWorldZ(nextPose.y));
                group.rotation.y = THREE.MathUtils.degToRad(Number(nextPose.rotate_z_deg || 0));
                this._refreshObjectStyles();
            } else {
                this._render();
            }
            if (typeof this.options.onPoseEdit === 'function') {
                this.options.onPoseEdit(buildItemIndex, nextPose);
            }
        }

        _applyPrimeTowerMove(nextPose) {
            this._updatePrimeTowerLocalPose(nextPose);
            this._render();
            if (typeof this.options.onPrimeTowerMove === 'function') {
                this.options.onPrimeTowerMove(nextPose);
            }
        }

        _intersectBedPlane(event) {
            if (!this.camera || !this.raycaster) return null;
            const rect = this.canvas.getBoundingClientRect();
            if (!rect.width || !rect.height) return null;
            this.pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
            this.pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
            this.raycaster.setFromCamera(this.pointer, this.camera);
            const out = new THREE.Vector3();
            const hit = this.raycaster.ray.intersectPlane(this.bedPlane, out);
            return hit ? out : null;
        }

        _orbit(dx, dy) {
            this.orbit.theta += dx * 0.008;
            this.orbit.phi += dy * 0.008;
            this.orbit.phi = Math.max(0.15, Math.min(Math.PI / 2 - 0.02, this.orbit.phi));
            this._updateCamera();
        }

        _pan(dx, dy) {
            const distance = this.camera.position.distanceTo(this.target);
            const scale = Math.max(0.02, distance / 1200);
            const forward = new THREE.Vector3();
            this.camera.getWorldDirection(forward);
            const right = new THREE.Vector3().crossVectors(forward, this.camera.up).normalize();
            const up = new THREE.Vector3().copy(this.camera.up).normalize();
            this.target.addScaledVector(right, -dx * scale);
            this.target.addScaledVector(up, dy * scale);
            this._updateCamera();
        }

        _updateCamera() {
            if (!this.camera) return;
            const r = this.orbit.radius;
            const x = this.target.x + r * Math.cos(this.orbit.phi) * Math.cos(this.orbit.theta);
            const z = this.target.z + r * Math.cos(this.orbit.phi) * Math.sin(this.orbit.theta);
            const y = this.target.y + r * Math.sin(this.orbit.phi);
            this.camera.position.set(x, y, z);
            this.camera.lookAt(this.target);
        }

        _resize() {
            if (this._initFailed) { this._render2D(); return; }
            if (!this.renderer || !this.camera) return;
            const container = this.canvas.parentElement || this.canvas;
            const w = Math.max(100, container.clientWidth || this.canvas.clientWidth || 100);
            const h = Math.max(180, container.clientHeight || this.canvas.clientHeight || 180);
            this.camera.aspect = w / h;
            this.camera.updateProjectionMatrix();
            this.renderer.setSize(w, h, false);
        }

        // 2D canvas fallback used when WebGL is unavailable (GPU sandbox disabled,
        // hardware acceleration off, or context limit exhausted).  Draws a top-down
        // schematic of the build plate with object footprints and prime tower.
        _render2D() {
            if (!this.canvas) return;
            const container = this.canvas.parentElement || this.canvas;
            const w = Math.max(100, container.clientWidth || this.canvas.clientWidth || 300);
            const h = Math.max(80, container.clientHeight || this.canvas.clientHeight || 200);
            if (this.canvas.width !== w || this.canvas.height !== h) {
                this.canvas.width = w;
                this.canvas.height = h;
            }
            const ctx = this.canvas.getContext('2d');
            if (!ctx) return;

            const vol = this.layout?.build_volume || { x: 270, y: 270 };
            const bedW = Math.max(1, Number(vol.x || 270));
            const bedD = Math.max(1, Number(vol.y || 270));

            // Banner height at top
            const bannerH = 22;
            const pad = 12;
            const scale = Math.min((w - 2 * pad) / bedW, (h - bannerH - 2 * pad) / bedD);
            const offX = (w - bedW * scale) / 2;
            const offY = bannerH + pad + ((h - bannerH - 2 * pad) - bedD * scale) / 2;

            // bed coords (mm) → canvas pixels
            const toC = (bx, by) => ({
                x: offX + bx * scale,
                y: offY + (bedD - by) * scale,
            });

            // Background
            ctx.fillStyle = '#1e293b';
            ctx.fillRect(0, 0, w, h);

            // Warning banner
            ctx.fillStyle = '#f59e0b';
            ctx.fillRect(0, 0, w, bannerH);
            ctx.fillStyle = '#1c1917';
            ctx.font = 'bold 11px system-ui, sans-serif';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText('3D viewer unavailable — restart browser to restore', w / 2, bannerH / 2);

            // Build plate
            const tl = toC(0, bedD);
            const br = toC(bedW, 0);
            ctx.fillStyle = '#f8fafc';
            ctx.fillRect(tl.x, tl.y, br.x - tl.x, br.y - tl.y);

            // Grid lines every 10 mm
            ctx.strokeStyle = '#e2e8f0';
            ctx.lineWidth = 0.5;
            for (let x = 0; x <= bedW; x += 10) {
                const p0 = toC(x, 0); const p1 = toC(x, bedD);
                ctx.beginPath(); ctx.moveTo(p0.x, p0.y); ctx.lineTo(p1.x, p1.y); ctx.stroke();
            }
            for (let y = 0; y <= bedD; y += 10) {
                const p0 = toC(0, y); const p1 = toC(bedW, y);
                ctx.beginPath(); ctx.moveTo(p0.x, p0.y); ctx.lineTo(p1.x, p1.y); ctx.stroke();
            }

            // Plate border
            ctx.strokeStyle = '#94a3b8';
            ctx.lineWidth = 1;
            ctx.strokeRect(tl.x, tl.y, br.x - tl.x, br.y - tl.y);

            // Objects
            const COLORS = ['#3b82f6', '#22c55e', '#ef4444', '#f59e0b', '#a855f7', '#06b6d4'];
            const objs = this.layout?.objects || [];
            for (let i = 0; i < objs.length; i++) {
                const obj = objs[i];
                const idx = Number(obj.build_item_index || 0);
                if (!idx) continue;

                const pose = (this.getPose && this.getPose(obj)) || { x: 0, y: 0, rotate_z_deg: 0 };
                const lb = obj.local_bounds || null;
                const size = (lb && Array.isArray(lb.size))
                    ? lb.size.map((v, k) => Math.max(k === 2 ? 1 : 4, Number(v || 0)))
                    : [20, 20, 10];
                const bmin = (lb && Array.isArray(lb.min)) ? lb.min : [0, 0, 0];
                const bmax = (lb && Array.isArray(lb.max)) ? lb.max : size;
                const cl = [
                    (Number(bmin[0]) + Number(bmax[0])) / 2,
                    (Number(bmin[1]) + Number(bmax[1])) / 2,
                ];

                const cx = toC(Number(pose.x || 0) + cl[0], Number(pose.y || 0) + cl[1]);
                const rx = size[0] * scale / 2;
                const ry = size[1] * scale / 2;
                const angle = -Number(pose.rotate_z_deg || 0) * Math.PI / 180;
                const isSelected = idx === this.selectedBuildItemIndex;
                const color = COLORS[i % COLORS.length];

                ctx.save();
                ctx.translate(cx.x, cx.y);
                ctx.rotate(angle);
                ctx.fillStyle = color + (isSelected ? 'cc' : '55');
                ctx.fillRect(-rx, -ry, rx * 2, ry * 2);
                ctx.strokeStyle = isSelected ? '#ffffff' : color;
                ctx.lineWidth = isSelected ? 2 : 1;
                ctx.strokeRect(-rx, -ry, rx * 2, ry * 2);
                const labelPx = Math.min(rx, ry, 14);
                if (labelPx >= 5) {
                    ctx.fillStyle = '#ffffff';
                    ctx.font = `bold ${Math.max(8, Math.min(13, labelPx))}px system-ui, sans-serif`;
                    ctx.textAlign = 'center';
                    ctx.textBaseline = 'middle';
                    ctx.fillText(String(idx), 0, 0);
                }
                ctx.restore();
            }

            // Prime tower
            if (this.primeTower) {
                const tw = Number(this.primeTower.width || 35);
                const brim = Number(this.primeTower.brim_width || 0);
                const tot = tw + 2 * brim;
                const px = Number(this.primeTower.x || 0);
                const py = Number(this.primeTower.y || 0);
                const ptl = toC(px, py + tot);
                ctx.fillStyle = '#94a3b833';
                ctx.strokeStyle = '#94a3b8';
                ctx.lineWidth = 1;
                const pw = tot * scale;
                ctx.fillRect(ptl.x, ptl.y, pw, pw);
                ctx.strokeRect(ptl.x, ptl.y, pw, pw);
                ctx.fillStyle = '#94a3b8';
                ctx.font = '9px system-ui, sans-serif';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillText('P', ptl.x + pw / 2, ptl.y + pw / 2);
            }
        }

        _clearGroup(group) {
            while (group && group.children.length) {
                const child = group.children.pop();
                if (!child) continue;
                group.remove(child);
                child.traverse?.((node) => {
                    if (node.geometry) node.geometry.dispose?.();
                    if (node.material) {
                        if (Array.isArray(node.material)) node.material.forEach((m) => m.dispose?.());
                        else node.material.dispose?.();
                    }
                    if (node.material?.map) node.material.map.dispose?.();
                });
            }
        }

        _rebuildScene() {
            if (!this.renderer) return;
            const t0 = (window.performance && typeof window.performance.now === 'function')
                ? window.performance.now()
                : Date.now();
            this._clearGroup(this.plateGroup);
            this._clearGroup(this.objectsGroup);
            this.objectMeshes.clear();
            this.boundsByIndex.clear();

            const vol = this.layout?.build_volume || { x: 270, y: 270, z: 270 };
            this.target.set(Number(vol.x || 270) / 2, 0, Number(vol.y || 270) / 2);
            this._updateCamera();
            this._buildPlate(vol);
            this._buildObjects();
            this._buildPrimeTower();
            this.refreshPoses();
            this._render();
            const t1 = (window.performance && typeof window.performance.now === 'function')
                ? window.performance.now()
                : Date.now();
            if (typeof this.options.onSceneStats === 'function') {
                let meshObjects = 0;
                let proxyObjects = 0;
                let triangles = 0;
                for (const obj of (this.geometryData?.objects || [])) {
                    if (obj?.has_mesh) meshObjects += 1;
                    else proxyObjects += 1;
                    triangles += Number(obj?.triangle_count || 0);
                }
                this.options.onSceneStats({
                    rebuild_ms: Math.round((t1 - t0) * 10) / 10,
                    object_count: (this.layout?.objects || []).length,
                    mesh_object_count: meshObjects,
                    proxy_object_count: Math.max(0, (this.layout?.objects || []).length - meshObjects),
                    triangle_count: triangles,
                    lod: this.geometryData?.lod || null,
                });
            }
        }

        _buildPlate(vol) {
            const w = Math.max(1, Number(vol.x || 270));
            const d = Math.max(1, Number(vol.y || 270));
            const h = Math.max(1, Number(vol.z || 270));

            const plane = new THREE.Mesh(
                new THREE.PlaneGeometry(w, d),
                new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.95, metalness: 0.0 })
            );
            plane.rotation.x = -Math.PI / 2;
            plane.position.set(w / 2, 0, d / 2);
            this.plateGroup.add(plane);

            const grid = new THREE.GridHelper(Math.max(w, d), Math.round(Math.max(w, d) / 10), 0x94a3b8, 0xe2e8f0);
            grid.position.set(w / 2, 0.05, d / 2);
            this.plateGroup.add(grid);

            const edge = new THREE.LineSegments(
                new THREE.EdgesGeometry(new THREE.BoxGeometry(w, h, d)),
                new THREE.LineBasicMaterial({ color: 0xcbd5e1 })
            );
            edge.position.set(w / 2, h / 2, d / 2);
            this.plateGroup.add(edge);

            const axes = new THREE.Group();
            axes.add(this._axisLine(0xef4444, new THREE.Vector3(0, 0.2, 0), new THREE.Vector3(20, 0.2, 0))); // X
            axes.add(this._axisLine(0x22c55e, new THREE.Vector3(0, 0.2, 0), new THREE.Vector3(0, 20, 0))); // Z(up)
            axes.add(this._axisLine(0x3b82f6, new THREE.Vector3(0, 0.2, 0), new THREE.Vector3(0, 0.2, -20))); // Y+
            axes.position.set(6, 0, d - 6);
            this.plateGroup.add(axes);
        }

        _axisLine(color, a, b) {
            const geom = new THREE.BufferGeometry().setFromPoints([a, b]);
            return new THREE.Line(geom, new THREE.LineBasicMaterial({ color }));
        }

        _buildObjects() {
            const objs = this.layout?.objects || [];
            for (const obj of objs) {
                const idx = Number(obj.build_item_index || 0);
                if (!idx) continue;

                const group = new THREE.Group();
                group.userData.buildItemIndex = idx;
                group.userData.object = obj;

                const localBounds = obj.local_bounds || null;
                const size = (localBounds && Array.isArray(localBounds.size))
                    ? localBounds.size.map((v, i) => Math.max(i === 2 ? 1 : 4, Number(v || 0) || (i === 2 ? 1 : 4)))
                    : [20, 20, 10];
                const bmin = (localBounds && Array.isArray(localBounds.min)) ? localBounds.min : [0, 0, 0];
                const bmax = (localBounds && Array.isArray(localBounds.max)) ? localBounds.max : size;
                const centerLocal = [
                    (Number(bmin[0] || 0) + Number(bmax[0] || 0)) / 2,
                    (Number(bmin[1] || 0) + Number(bmax[1] || 0)) / 2,
                    (Number(bmin[2] || 0) + Number(bmax[2] || 0)) / 2,
                ];
                this.boundsByIndex.set(idx, { centerLocal, size });

                const geomEntry = this.geometryByIndex.get(idx);
                const builtMesh = this._buildActualMesh(group, idx, geomEntry);
                if (!builtMesh) {
                    this._buildProxyMesh(group, idx, centerLocal, size);
                }

                const labelSprite = this._makeLabelSprite(String(idx));
                labelSprite.userData.buildItemIndex = idx;
                group.add(labelSprite);
                group.userData.labelSprite = labelSprite;

                this.objectsGroup.add(group);
                this.objectMeshes.set(idx, group);
            }
        }

        _buildPrimeTower() {
            this.primeTowerGroup = null;
            if (!this.primeTower) return;

            const width = Math.max(10, Number(this.primeTower.width || 35));
            const brim = Math.max(0, Number(this.primeTower.brim_width || 0));
            const footprint = Number(this.primeTower.footprint_w || (width + (2 * brim)));
            const footprintDepth = Number(this.primeTower.footprint_h || footprint);
            const height = 22;
            const anchorSemantics = !!this.primeTower.anchor_is_slicer_coords;
            const localOffsetX = anchorSemantics ? (width / 2) : 0;
            const localOffsetZ = anchorSemantics ? (-(footprintDepth / 2)) : 0;

            const group = new THREE.Group();
            group.userData.kind = 'primeTower';

            const base = new THREE.Mesh(
                new THREE.BoxGeometry(footprint, 0.8, footprint),
                new THREE.MeshStandardMaterial({ color: 0xfdba74, transparent: true, opacity: 0.35, roughness: 0.95 })
            );
            base.scale.set(1, 1, footprintDepth / Math.max(1, footprint));
            base.position.set(localOffsetX, 0.4, localOffsetZ);
            base.userData.kind = 'primeTower';
            group.add(base);

            const tower = new THREE.Mesh(
                new THREE.BoxGeometry(width, height, width),
                new THREE.MeshStandardMaterial({ color: 0xf97316, transparent: true, opacity: 0.55, roughness: 0.8 })
            );
            tower.position.set(localOffsetX, (height / 2) + 0.8, localOffsetZ);
            tower.userData.kind = 'primeTower';
            group.add(tower);
            group.userData.solid = tower;

            const wire = new THREE.LineSegments(
                new THREE.EdgesGeometry(new THREE.BoxGeometry(width, height, width)),
                new THREE.LineBasicMaterial({ color: 0xea580c })
            );
            wire.position.copy(tower.position);
            wire.userData.kind = 'primeTower';
            group.add(wire);
            group.userData.wire = wire;

            if (anchorSemantics) {
                const anchor = new THREE.LineSegments(
                    new THREE.BufferGeometry().setFromPoints([
                        new THREE.Vector3(-4, 0.6, 0), new THREE.Vector3(4, 0.6, 0),
                        new THREE.Vector3(0, 0.6, -4), new THREE.Vector3(0, 0.6, 4),
                    ]),
                    new THREE.LineBasicMaterial({ color: 0xfef3c7 })
                );
                anchor.userData.kind = 'primeTower';
                group.add(anchor);
                group.userData.anchorMarker = anchor;
            }

            const label = this._makeLabelSprite(this.primeTower?.explicit_position ? 'PT' : 'PT*');
            label.position.set(localOffsetX, height + 10, localOffsetZ);
            label.userData.kind = 'primeTower';
            group.add(label);
            group.userData.labelSprite = label;

            this.objectsGroup.add(group);
            this.primeTowerGroup = group;
            this._updatePrimeTowerLocalPose(this.primeTower);
        }

        _updatePrimeTowerLocalPose(pose) {
            if (!this.primeTowerGroup) return;
            this.primeTowerGroup.position.set(Number(pose?.x || 0), 0, this._bedYToWorldZ(pose?.y));
        }

        _refreshPrimeTowerPose() {
            if (!this.primeTowerGroup) return;
            this._updatePrimeTowerLocalPose(this.primeTower);
            const label = this.primeTowerGroup.userData.labelSprite;
            if (label && this.primeTower) {
                const hasExplicit = !!this.primeTower.explicit_position;
                label.visible = true;
                label.scale.set(16, 8, 1);
                // Rebuild label only when needed (simple path)
                if (label.userData._primeLabelText !== (hasExplicit ? 'PT' : 'PT*')) {
                    this.primeTowerGroup.remove(label);
                    this.primeTowerGroup.userData.labelSprite = this._makeLabelSprite(hasExplicit ? 'PT' : 'PT*');
                    const nextLabel = this.primeTowerGroup.userData.labelSprite;
                    const solid = this.primeTowerGroup.userData.solid;
                    nextLabel.position.set(
                        Number(solid?.position?.x || 0),
                        32,
                        Number(solid?.position?.z || 0),
                    );
                    nextLabel.userData.kind = 'primeTower';
                    this.primeTowerGroup.add(nextLabel);
                }
            }
        }

        _buildActualMesh(group, idx, geomEntry) {
            if (!geomEntry || !geomEntry.has_mesh || !Array.isArray(geomEntry.vertices) || !Array.isArray(geomEntry.triangles)) {
                return false;
            }
            if (!geomEntry.vertices.length || !geomEntry.triangles.length) return false;

            try {
                const positions = new Float32Array(geomEntry.vertices.length * 3);
                for (let i = 0; i < geomEntry.vertices.length; i++) {
                    const v = geomEntry.vertices[i] || [0, 0, 0];
                    // 3MF coords: x,y,z -> Three coords: x,z,-y (Y up)
                    // to match gcode-preview's bed-axis visual orientation.
                    positions[i * 3 + 0] = Number(v[0] || 0);
                    positions[i * 3 + 1] = Number(v[2] || 0);
                    positions[i * 3 + 2] = -Number(v[1] || 0);
                }
                const indexCount = geomEntry.triangles.length * 3;
                const use32 = geomEntry.vertices.length > 65535;
                const indexArr = use32 ? new Uint32Array(indexCount) : new Uint16Array(indexCount);
                let p = 0;
                for (const tri of geomEntry.triangles) {
                    indexArr[p++] = Number(tri?.[0] || 0);
                    indexArr[p++] = Number(tri?.[1] || 0);
                    indexArr[p++] = Number(tri?.[2] || 0);
                }

                const geom = new THREE.BufferGeometry();
                geom.setAttribute('position', new THREE.BufferAttribute(positions, 3));
                geom.setIndex(new THREE.BufferAttribute(indexArr, 1));
                geom.computeVertexNormals();
                geom.computeBoundingBox();

                const solid = new THREE.Mesh(
                    geom,
                    new THREE.MeshStandardMaterial({
                        color: 0xbfdbfe,
                        roughness: 0.8,
                        metalness: 0.03,
                        transparent: true,
                        opacity: 0.92,
                        side: THREE.DoubleSide,
                    }),
                );
                solid.userData.buildItemIndex = idx;
                group.add(solid);
                group.userData.meshSolid = solid;

                const wire = new THREE.LineSegments(
                    new THREE.EdgesGeometry(geom),
                    new THREE.LineBasicMaterial({ color: 0x2563eb })
                );
                wire.userData.buildItemIndex = idx;
                solid.add(wire);
                group.userData.wire = wire;
                return true;
            } catch (err) {
                console.warn('Placement mesh render fallback to proxy:', err);
                return false;
            }
        }

        _buildProxyMesh(group, idx, centerLocal, size) {
            const proxy = new THREE.Mesh(
                new THREE.BoxGeometry(size[0], size[2], size[1]),
                new THREE.MeshStandardMaterial({
                    color: 0x93c5fd,
                    transparent: true,
                    opacity: 0.55,
                    roughness: 0.75,
                    metalness: 0.05,
                })
            );
            proxy.userData.buildItemIndex = idx;
            group.add(proxy);
            group.userData.proxy = proxy;

            const wire = new THREE.LineSegments(
                new THREE.EdgesGeometry(new THREE.BoxGeometry(size[0], size[2], size[1])),
                new THREE.LineBasicMaterial({ color: 0x2563eb })
            );
            wire.userData.buildItemIndex = idx;
            proxy.add(wire);
            group.userData.wire = wire;
            proxy.position.set(Number(centerLocal[0] || 0), Number(size[2] || 10) / 2, this._bedLocalYToLocalZ(centerLocal[1] || 0));
        }

        _makeLabelSprite(text) {
            const canvas = document.createElement('canvas');
            canvas.width = 96;
            canvas.height = 48;
            const ctx = canvas.getContext('2d');
            if (!ctx) return new THREE.Sprite(new THREE.SpriteMaterial());
            ctx.fillStyle = 'rgba(15,23,42,0.88)';
            ctx.strokeStyle = 'rgba(255,255,255,0.92)';
            ctx.lineWidth = 2;
            ctx.beginPath();
            const r = 12;
            ctx.moveTo(r, 0);
            ctx.lineTo(canvas.width - r, 0);
            ctx.quadraticCurveTo(canvas.width, 0, canvas.width, r);
            ctx.lineTo(canvas.width, canvas.height - r);
            ctx.quadraticCurveTo(canvas.width, canvas.height, canvas.width - r, canvas.height);
            ctx.lineTo(r, canvas.height);
            ctx.quadraticCurveTo(0, canvas.height, 0, canvas.height - r);
            ctx.lineTo(0, r);
            ctx.quadraticCurveTo(0, 0, r, 0);
            ctx.closePath();
            ctx.fill();
            ctx.stroke();
            ctx.fillStyle = '#ffffff';
            ctx.font = 'bold 24px sans-serif';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(text, canvas.width / 2, canvas.height / 2 + 1);

            const tex = new THREE.CanvasTexture(canvas);
            tex.needsUpdate = true;
            const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false });
            const sprite = new THREE.Sprite(mat);
            sprite.scale.set(16, 8, 1);
            sprite.userData._primeLabelText = text;
            return sprite;
        }

        _refreshObjectStyles() {
            for (const [idx, group] of this.objectMeshes.entries()) {
                const selected = Number(idx) === Number(this.selectedBuildItemIndex || 0);
                const solid = group.userData.meshSolid;
                const proxy = group.userData.proxy;
                const wire = group.userData.wire;

                if (solid?.material) {
                    solid.material.color.set(selected ? 0x60a5fa : 0xbfdbfe);
                    solid.material.opacity = selected ? 0.98 : 0.92;
                    solid.material.emissive = new THREE.Color(selected ? 0x0f172a : 0x000000);
                    solid.material.emissiveIntensity = selected ? 0.08 : 0.0;
                }
                if (proxy?.material) {
                    proxy.material.color.set(selected ? 0x3b82f6 : 0x93c5fd);
                    proxy.material.opacity = selected ? 0.75 : 0.50;
                }
                if (wire?.material) {
                    wire.material.color.set(selected ? 0x1d4ed8 : 0x2563eb);
                }
                const label = group.userData.labelSprite;
                if (label) label.scale.set(selected ? 18 : 16, selected ? 9 : 8, 1);
            }
            if (this.primeTowerGroup) {
                const solid = this.primeTowerGroup.userData.solid;
                const wire = this.primeTowerGroup.userData.wire;
                if (solid?.material) {
                    solid.material.color.set(0xf97316);
                    solid.material.opacity = 0.55;
                }
                if (wire?.material) {
                    wire.material.color.set(0xea580c);
                }
            }
            this._render();
        }

        _pickObjectHit(event) {
            if (!this.camera || !this.raycaster) return null;
            const rect = this.canvas.getBoundingClientRect();
            if (!rect.width || !rect.height) return null;
            this.pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
            this.pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
            this.raycaster.setFromCamera(this.pointer, this.camera);

            const pickables = [];
            for (const group of this.objectMeshes.values()) {
                if (group.userData.meshSolid) pickables.push(group.userData.meshSolid);
                if (group.userData.proxy) pickables.push(group.userData.proxy);
                if (group.userData.labelSprite) pickables.push(group.userData.labelSprite);
            }
            if (this.primeTowerGroup) {
                if (this.primeTowerGroup.userData.solid) pickables.push(this.primeTowerGroup.userData.solid);
                if (this.primeTowerGroup.userData.labelSprite) pickables.push(this.primeTowerGroup.userData.labelSprite);
            }

            const hits = this.raycaster.intersectObjects(pickables, true);
            if (!hits.length) return null;
            for (const hit of hits) {
                let node = hit.object;
                while (node) {
                    if (node.userData && node.userData.kind === 'primeTower') {
                        return {
                            kind: 'primeTower',
                            point: hit.point?.clone?.() || null,
                        };
                    }
                    if (node.userData && node.userData.buildItemIndex) {
                        return {
                            buildItemIndex: Number(node.userData.buildItemIndex),
                            point: hit.point?.clone?.() || null,
                        };
                    }
                    node = node.parent;
                }
            }
            return null;
        }

        _startLoop() {
            if (this._animating) return;
            this._animating = true;
            this._raf = requestAnimationFrame(this._boundAnimate);
        }

        _stopLoop() {
            this._animating = false;
            if (this._raf) cancelAnimationFrame(this._raf);
            this._raf = null;
        }

        _animate() {
            if (!this._animating) return;
            for (const group of this.objectMeshes.values()) {
                const label = group.userData.labelSprite;
                if (label && this.camera) label.quaternion.copy(this.camera.quaternion);
            }
            if (this.primeTowerGroup?.userData?.labelSprite && this.camera) {
                this.primeTowerGroup.userData.labelSprite.quaternion.copy(this.camera.quaternion);
            }
            this._render();
            this._raf = requestAnimationFrame(this._boundAnimate);
        }

        _render() {
            if (!this.renderer || !this.scene || !this.camera) return;
            this.renderer.render(this.scene, this.camera);
        }
    }

    window.MeshPlacementViewer = MeshPlacementViewer;
})();
