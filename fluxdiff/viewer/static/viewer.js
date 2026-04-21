// PCB Diff Viewer: Zoom and Pan functionality

(function () {
    // Config
    const MIN_ZOOM = 0.2;
    const MAX_ZOOM = 4;
    const ZOOM_STEP = 0.1;

    // Assumption: PCB images have the CSS class "pcb-image"
    const images = Array.from(document.querySelectorAll('.pcb-image'));

    if (!images.length) return; // No images found, do nothing

    // Create a wrapper div for styling/transform if not present
    images.forEach(image => {
        if (!image.parentElement.classList.contains('pcb-image-wrap')) {
            const wrap = document.createElement('div');
            wrap.className = 'pcb-image-wrap';
            wrap.style.position = 'relative';
            wrap.style.overflow = 'hidden';
            wrap.style.display = 'inline-block';
            wrap.style.width = image.width + 'px';
            wrap.style.height = image.height + 'px';
            image.parentElement.insertBefore(wrap, image);
            wrap.appendChild(image);
            // for correct transform origin
            image.style.display = 'block';
            image.style.position = 'absolute';
            image.style.top = '0px';
            image.style.left = '0px';
            image.style.transformOrigin = '0 0';
            image.draggable = false;
        }
    });

    // State
    let zoom = 1;
    let panX = 0;
    let panY = 0;

    let isPanning = false;
    let startX = 0, startY = 0;

    // Utility: Apply transform to all images
    function updateTransforms() {
        images.forEach(image => {
            image.style.transform = `scale(${zoom}) translate(${panX / zoom}px, ${panY / zoom}px)`;
        });
    }

    // Zoom handling
    function onWheel(e) {
        // Ctrl+wheel default: zoom browser, ignore it
        if (e.ctrlKey) return;
        e.preventDefault();

        const prevZoom = zoom;
        const delta = e.deltaY || e.detail || e.wheelDelta;
        // Wheel up = zoom in, down = zoom out
        const direction = delta > 0 ? -1 : 1;
        let newZoom = zoom + direction * ZOOM_STEP;
        newZoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, newZoom));
        if (newZoom === zoom) return;

        // (Optional) Zoom to mouse position - let's keep it simple: adjust pan to center zoom under cursor
        // Calculated relative to image 0 for reference
        const rect = images[0].getBoundingClientRect();
        const mouseX = e.clientX - rect.left;
        const mouseY = e.clientY - rect.top;

        // Adjust pan so the point under cursor remains at the same image point
        panX = (panX + mouseX) * (newZoom / prevZoom) - mouseX;
        panY = (panY + mouseY) * (newZoom / prevZoom) - mouseY;

        zoom = newZoom;
        updateTransforms();
    }

    // Pan handling
    function onMouseDown(e) {
        // Only left button
        if (e.button !== 0) return;
        e.preventDefault();
        isPanning = true;
        startX = e.clientX - panX;
        startY = e.clientY - panY;
        document.body.style.cursor = 'grabbing';
    }
    function onMouseMove(e) {
        if (!isPanning) return;
        panX = e.clientX - startX;
        panY = e.clientY - startY;
        updateTransforms();
    }
    function onMouseUp(e) {
        if (!isPanning) return;
        isPanning = false;
        document.body.style.cursor = '';
    }

    // Init: Attach handlers to the wrapper of image 0 for events
    const wrapper = images[0].parentElement;

    wrapper.addEventListener('wheel', onWheel, { passive: false });
    wrapper.addEventListener('mousedown', onMouseDown);
    window.addEventListener('mousemove', onMouseMove);
    window.addEventListener('mouseup', onMouseUp);

    // Optional: Reset on double-click
    wrapper.addEventListener('dblclick', function () {
        zoom = 1;
        panX = panY = 0;
        updateTransforms();
    });

    // Style tweaks for panning cursor (optional, lightweight)
    const style = document.createElement('style');
    style.textContent = `
        .pcb-image-wrap { cursor: grab; }
        .pcb-image-wrap img { user-select: none; }
    `;
    document.head.appendChild(style);

    // Set initial transform
    updateTransforms();
})();