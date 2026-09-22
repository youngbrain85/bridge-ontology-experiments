import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

const FILE_PATH = /^\/files\/[A-Za-z0-9][A-Za-z0-9_-]{0,79}\/S[0-9]{3,}\/model\.glb$/;
class ViewerError extends Error {}
const DEFAULT_MESSAGE = 'Select a model to display it here.';
const FIT_CAPTION = 'Each model is fitted independently; on-screen size does not indicate relative physical dimensions.';

function disposeObject(object) {
  const geometries = new Set();
  const materials = new Set();
  const textures = new Set();
  object?.traverse((item) => {
    if (item.geometry) geometries.add(item.geometry);
    const items = Array.isArray(item.material) ? item.material : [item.material];
    for (const material of items) {
      if (!material) continue;
      materials.add(material);
      for (const value of Object.values(material)) if (value?.isTexture) textures.add(value);
    }
  });
  for (const texture of textures) {
    texture.dispose();
    if (typeof texture.image?.close === 'function') texture.image.close();
  }
  for (const material of materials) material.dispose();
  for (const geometry of geometries) geometry.dispose();
}

function localModelUrl(value) {
  if (typeof value !== 'string') throw new ViewerError('Check the review model path.');
  const url = new URL(value, window.location.href);
  if (url.origin !== window.location.origin || !['http:', 'https:'].includes(url.protocol) || !FILE_PATH.test(url.pathname) || url.search || url.hash || url.username || url.password) {
    throw new ViewerError('Only review models provided by this app can be opened here.');
  }
  return url.href;
}

function checkStandaloneGlb(buffer) {
  if (buffer.byteLength < 20) throw new ViewerError('The GLB file format could not be read.');
  const data = new DataView(buffer);
  if (data.getUint32(0, true) !== 0x46546c67 || data.getUint32(4, true) !== 2 || data.getUint32(8, true) !== buffer.byteLength) {
    throw new ViewerError('This is not a valid GLB 2.0 file.');
  }
  let offset = 12;
  let document = null;
  while (offset < buffer.byteLength) {
    if (offset + 8 > buffer.byteLength) throw new ViewerError('Part of the GLB file is missing.');
    const length = data.getUint32(offset, true);
    const type = data.getUint32(offset + 4, true);
    offset += 8;
    if (length % 4 || offset + length > buffer.byteLength) throw new ViewerError('Part of the GLB file is missing.');
    if (type === 0x4e4f534a) {
      if (document !== null) throw new ViewerError('The GLB contains duplicate internal documents.');
      try {
        document = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, offset, length)));
      } catch {
        throw new ViewerError('The internal GLB document could not be read.');
      }
    }
    offset += length;
  }
  if (!document || typeof document !== 'object') throw new ViewerError('The GLB has no internal document.');
  // Only the downloaded file may supply geometry and images. No URI, even a
  // same-origin relative file, may request a second model dependency.
  for (const entry of [...(document.buffers || []), ...(document.images || [])]) {
    if (Object.prototype.hasOwnProperty.call(entry, 'uri')) {
      throw new ViewerError('GLB files referencing external resources cannot be displayed.');
    }
  }
}

export function createModelViewer(container) {
  if (!container || typeof container.appendChild !== 'function') throw new TypeError('A viewer container is required.');
  const root = document.createElement('div');
  root.className = 'local-model-viewer';
  root.style.cssText = 'position:relative;width:100%;height:100%;min-height:320px;overflow:hidden;border-radius:14px;background:#eef3f7;isolation:isolate;';
  root.dataset.viewerState = 'empty';
  const status = document.createElement('div');
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  status.style.cssText = 'position:absolute;inset:0;display:flex;align-items:center;justify-content:center;padding:36px;text-align:center;color:#45556a;font:500 15px/1.7 system-ui,sans-serif;pointer-events:none;z-index:2;';
  status.textContent = DEFAULT_MESSAGE;
  const caption = document.createElement('div');
  caption.style.cssText = 'position:absolute;left:12px;right:12px;bottom:10px;padding:8px 10px;border-radius:8px;background:rgba(248,251,253,.92);color:#536276;font:12px/1.55 system-ui,sans-serif;pointer-events:none;z-index:2;';
  caption.textContent = FIT_CAPTION;
  caption.hidden = true;
  const hint = document.createElement('div');
  hint.style.cssText = 'position:absolute;top:12px;left:12px;right:12px;color:#65768b;font:12px/1.5 system-ui,sans-serif;pointer-events:none;z-index:2;';
  hint.textContent = 'Drag to orbit · Scroll to zoom · Right-drag to pan';
  hint.hidden = true;
  root.append(status, hint, caption);
  container.appendChild(root);

  let disposed = false;
  let renderer = null;
  let controls = null;
  let observer = null;
  let animationFrame = 0;
  let model = null;
  let requestToken = 0;
  let abortController = null;
  let contextLost = false;
  let fittedRadius = 1;
  let modelCenter = new THREE.Vector3();
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0xeef3f7);
  const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.01, 100);
  camera.position.set(3, 2, 3);
  camera.up.set(0, 1, 0);
  const hemisphere = new THREE.HemisphereLight(0xffffff, 0x718095, 2.1);
  const key = new THREE.DirectionalLight(0xffffff, 2.5);
  key.position.set(4, 8, 5);
  const fill = new THREE.DirectionalLight(0xffffff, 1.2);
  fill.position.set(-4, 2, -3);
  scene.add(hemisphere, key, fill);

  const setStatus = (state, message) => {
    root.dataset.viewerState = state;
    status.textContent = message;
    status.style.display = message ? 'flex' : 'none';
    caption.hidden = state !== 'loaded';
    hint.hidden = state !== 'loaded';
  };

  const requestRender = () => {
    if (disposed || contextLost || !renderer || animationFrame) return;
    animationFrame = requestAnimationFrame(() => {
      animationFrame = 0;
      if (disposed || contextLost || !renderer) return;
      const changed = controls?.update();
      renderer.render(scene, camera);
      if (changed) requestRender();
    });
  };

  function resize() {
    if (disposed || !renderer) return;
    const rect = root.getBoundingClientRect();
    const width = Math.max(1, rect.width || container.clientWidth || 640);
    const height = Math.max(1, rect.height || 360);
    const aspect = width / height;
    const half = fittedRadius * 1.22;
    camera.left = -half * Math.max(1, aspect);
    camera.right = -camera.left;
    camera.top = half * Math.max(1, 1 / aspect);
    camera.bottom = -camera.top;
    camera.updateProjectionMatrix();
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(width, height, false);
    requestRender();
  }

  function setView(view) {
    if (disposed || !renderer || !controls || !model || !['iso', 'front', 'top'].includes(view)) return;
    const direction = view === 'top' ? new THREE.Vector3(0, 1, 0) : view === 'front' ? new THREE.Vector3(0, 0, 1) : new THREE.Vector3(1, 0.7, 1).normalize();
    camera.up.set(0, view === 'top' ? 0 : 1, view === 'top' ? -1 : 0);
    camera.position.copy(modelCenter).addScaledVector(direction, fittedRadius * 4);
    camera.near = Math.max(fittedRadius * 0.00001, Number.EPSILON);
    camera.far = fittedRadius * 100;
    camera.zoom = 1;
    controls.target.copy(modelCenter);
    camera.lookAt(modelCenter);
    controls.update();
    resize();
  }

  function clearModel() {
    if (model) {
      scene.remove(model);
      disposeObject(model);
      model = null;
    }
    root.dataset.meshCount = '0';
    requestRender();
  }

  function clear() {
    if (disposed) return;
    requestToken += 1;
    abortController?.abort();
    abortController = null;
    clearModel();
    setStatus(renderer && !contextLost ? 'empty' : 'unavailable', renderer && !contextLost ? DEFAULT_MESSAGE : '3D display is unavailable in this browser. Download the GLB or DXF file to review it.');
  }

  const onContextLost = (event) => {
    event.preventDefault();
    contextLost = true;
    requestToken += 1;
    abortController?.abort();
    abortController = null;
    if (animationFrame) cancelAnimationFrame(animationFrame);
    animationFrame = 0;
    setStatus('unavailable', 'The 3D display context was lost. Reopen this page or download the model file.');
  };

  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, preserveDrawingBuffer: false });
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1;
    renderer.domElement.style.cssText = 'display:block;width:100%;height:100%;outline:none;touch-action:none;';
    renderer.domElement.setAttribute('aria-label', '3D model. Drag to orbit, scroll to zoom, and right-drag to pan.');
    renderer.domElement.tabIndex = 0;
    root.prepend(renderer.domElement);
    renderer.domElement.addEventListener('webglcontextlost', onContextLost);
    controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.12;
    controls.minZoom = 0.001;
    controls.maxZoom = 10000;
    controls.addEventListener('change', requestRender);
    controls.listenToKeyEvents(renderer.domElement);
    if (typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(resize);
      observer.observe(root);
    } else {
      window.addEventListener('resize', resize);
    }
    resize();
  } catch {
    controls?.dispose();
    renderer?.dispose();
    renderer?.domElement.remove();
    controls = null;
    renderer = null;
    setStatus('unavailable', '3D display is unavailable in this browser. Download the GLB or DXF file to review it.');
  }

  async function load(url) {
    if (disposed) return { status: 'disposed' };
    if (!renderer || contextLost) return { status: 'unavailable' };
    const token = ++requestToken;
    abortController?.abort();
    abortController = new AbortController();
    const signal = abortController.signal;
    clearModel();
    setStatus('loading', 'Loading the model.');
    try {
      const safeUrl = localModelUrl(url);
      const response = await fetch(safeUrl, { signal, credentials: 'same-origin', redirect: 'error', cache: 'no-store' });
      if (!response.ok) throw new ViewerError('The model file could not be loaded. Check its conversion status and file availability.');
      const buffer = await response.arrayBuffer();
      if (disposed || token !== requestToken) return { status: 'cancelled' };
      checkStandaloneGlb(buffer);
      const manager = new THREE.LoadingManager();
      manager.setURLModifier((resource) => {
        // GLTFLoader creates local blob URLs for images embedded in this GLB.
        if (resource.startsWith('blob:' + window.location.origin + '/')) return resource;
        throw new ViewerError('Resources outside the model file are not loaded.');
      });
      const loader = new GLTFLoader(manager);
      const gltf = await loader.parseAsync(buffer, '');
      if (disposed || token !== requestToken) {
        disposeObject(gltf.scene);
        return { status: 'cancelled' };
      }
      const candidate = gltf.scene;
      let meshCount = 0;
      candidate.updateMatrixWorld(true);
      candidate.traverse((item) => {
        if (item.isMesh && item.geometry?.getAttribute('position')?.count > 0) meshCount += 1;
      });
      const bounds = new THREE.Box3().setFromObject(candidate, true);
      if (!meshCount || bounds.isEmpty() || ![...bounds.min.toArray(), ...bounds.max.toArray()].every(Number.isFinite)) {
        disposeObject(candidate);
        setStatus('empty', 'No displayable geometry. Check the conversion status.');
        return { status: 'empty' };
      }
      modelCenter = bounds.getCenter(new THREE.Vector3());
      fittedRadius = Math.max(bounds.getSize(new THREE.Vector3()).length() / 2, 1e-8);
      model = candidate;
      scene.add(model);
      root.dataset.meshCount = String(meshCount);
      setView('iso');
      setStatus('loaded', '');
      requestRender();
      return { status: 'loaded' };
    } catch (error) {
      if (disposed || token !== requestToken || error?.name === 'AbortError') return { status: 'cancelled' };
      // Never expose model labels, absolute paths, URLs, or loader diagnostics.
      const message = error instanceof ViewerError ? error.message : 'This model could not be displayed. Check its file format and conversion status.';
      setStatus('failed', message);
      return { status: 'failed' };
    } finally {
      if (token === requestToken) abortController = null;
    }
  }

  function dispose() {
    if (disposed) return;
    requestToken += 1;
    abortController?.abort();
    abortController = null;
    disposed = true;
    if (animationFrame) cancelAnimationFrame(animationFrame);
    animationFrame = 0;
    observer?.disconnect();
    window.removeEventListener('resize', resize);
    controls?.removeEventListener('change', requestRender);
    controls?.dispose();
    clearModel();
    if (renderer) {
      renderer.domElement.removeEventListener('webglcontextlost', onContextLost);
      renderer.renderLists.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
      renderer.domElement.remove();
      renderer = null;
    }
    scene.clear();
    root.remove();
  }

  return { load, clear, setView, resize, dispose };
}
