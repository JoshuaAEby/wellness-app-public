// Shrink a photo before upload — shared by the Log Food scanner and the My Food form
// (note 40). A phone photo is 3–12 MB and the label parser only needs the text legible;
// 1600 px on the long edge keeps small print readable at a fraction of the upload.
// Also normalises HEIC/PNG to JPEG, which the API accepts.
export const shrinkImage = (file, maxEdge = 1600, quality = 0.82) =>
  new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      const scale = Math.min(1, maxEdge / Math.max(img.width, img.height));
      const c = document.createElement("canvas");
      c.width = Math.round(img.width * scale);
      c.height = Math.round(img.height * scale);
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      c.toBlob(b => (b ? resolve(b) : reject(new Error("Could not read image"))),
               "image/jpeg", quality);
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("Could not read image")); };
    img.src = url;
  });

// Blood-panel screenshots (2026-10-28). The API downsizes anything past 1568 px on the
// long edge anyway, so sending more only costs upload time. A normal phone screenshot
// (1179×2556) lands at 723×1568 — a 0.61 scale that keeps table text legible. A
// scrolling capture (aspect > 2.6) would shrink to an unreadable sliver, so it is cut
// into overlapping tiles first; the parser is told screenshots overlap and the server
// folds repeated rows (blood_reconcile.collapse_duplicates). Returns one entry per tile,
// in reading order. Rejects if the browser can't decode the file (HEIC off Safari).
export const BLOOD_IMG_EDGE = 1568;
const TILE_ASPECT = 2.2, SLICE_OVER = 2.6, TILE_OVERLAP = 0.15;
export const prepareScreenshot = (file) =>
  new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = async () => {
      URL.revokeObjectURL(url);
      const W = img.naturalWidth, H = img.naturalHeight;
      const tiles = [];
      if (H / W > SLICE_OVER) {
        const th = Math.round(W * TILE_ASPECT), step = Math.round(th * (1 - TILE_OVERLAP));
        for (let y = 0; ; y += step) {
          const top = Math.min(y, H - th);
          tiles.push([top, th]);
          if (top + th >= H) break;
        }
      } else {
        tiles.push([0, H]);
      }
      try {
        const out = [];
        for (const [top, h] of tiles) {
          const scale = Math.min(1, BLOOD_IMG_EDGE / Math.max(W, h));
          const c = document.createElement("canvas");
          c.width = Math.round(W * scale);
          c.height = Math.round(h * scale);
          const ctx = c.getContext("2d");
          ctx.fillStyle = "#fff";            // transparent PNG regions → white, not black JPEG
          ctx.fillRect(0, 0, c.width, c.height);
          ctx.drawImage(img, 0, top, W, h, 0, 0, c.width, c.height);
          const blob = await new Promise(r => c.toBlob(r, "image/jpeg", 0.9));
          if (!blob) throw new Error("encode");
          out.push({ blob, width: c.width, height: c.height });
        }
        resolve(out);
      } catch {
        reject(new Error(`Could not convert ${file.name}`));
      }
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      const heic = /\.hei[cf]$/i.test(file.name) || /hei[cf]/i.test(file.type);
      reject(new Error(heic
        ? `${file.name}: this browser can't open HEIC — use Safari on the phone, or export as JPEG`
        : `${file.name}: not a readable image`));
    };
    img.src = url;
  });
