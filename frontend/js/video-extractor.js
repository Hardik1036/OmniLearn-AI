/* OmniLearn AI — Reusable Single-<canvas> Multi-Window Video Keyframe Slicer */

import { ApiError } from './api-client.js';

export function getLayoutRois(layoutType = '2x2') {
  switch (layoutType) {
    case '1x2':
      return [
        { roi_key: 'q1', name: 'Left Window', ymin: 0, ymax: 1000, xmin: 0, xmax: 500, color: '#3B82F6' },
        { roi_key: 'q2', name: 'Right Window', ymin: 0, ymax: 1000, xmin: 500, xmax: 1000, color: '#10B981' },
      ];
    case '2x1':
      return [
        { roi_key: 'q1', name: 'Top Window', ymin: 0, ymax: 500, xmin: 0, xmax: 1000, color: '#3B82F6' },
        { roi_key: 'q2', name: 'Bottom Window', ymin: 500, ymax: 1000, xmin: 0, xmax: 1000, color: '#8B5CF6' },
      ];
    case '1x1':
      return [
        { roi_key: 'q1', name: 'Full Screen', ymin: 0, ymax: 1000, xmin: 0, xmax: 1000, color: '#3B82F6' },
      ];
    case '2x2':
    default:
      return [
        { roi_key: 'q1', name: 'Top-Left Window', ymin: 0, ymax: 500, xmin: 0, xmax: 500, color: '#3B82F6' },
        { roi_key: 'q2', name: 'Top-Right Window', ymin: 0, ymax: 500, xmin: 500, xmax: 1000, color: '#10B981' },
        { roi_key: 'q3', name: 'Bottom-Left Window', ymin: 500, ymax: 1000, xmin: 0, xmax: 500, color: '#F59E0B' },
        { roi_key: 'q4', name: 'Bottom-Right Window', ymin: 500, ymax: 1000, xmin: 500, xmax: 1000, color: '#8B5CF6' },
      ];
  }
}

function sampleLuminanceSignature(imageData, sampleCount = 64) {
  const data = imageData.data;
  const totalPixels = data.length / 4;
  if (!totalPixels) {
    return new Uint8Array(0);
  }
  const stride = Math.max(1, Math.floor(totalPixels / sampleCount));
  const sig = new Uint8Array(Math.min(sampleCount, totalPixels));
  let idx = 0;
  for (let p = 0; p < totalPixels && idx < sig.length; p += stride) {
    const offset = p * 4;
    // Fast integer luminance approximation
    sig[idx++] = (data[offset] * 3 + data[offset + 1] * 6 + data[offset + 2]) / 10;
  }
  return sig;
}

function computeSignatureDelta(prevSig, nextSig) {
  if (!prevSig || prevSig.length !== nextSig.length || nextSig.length === 0) {
    return 255;
  }
  let diffSum = 0;
  for (let i = 0; i < nextSig.length; i++) {
    diffSum += Math.abs(nextSig[i] - prevSig[i]);
  }
  return diffSum / nextSig.length;
}

function waitForEvent(target, eventName, errorEventName = 'error', timeoutMs = 15000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      cleanup();
      reject(new Error(`Timed out waiting for video ${eventName}`));
    }, timeoutMs);

    function onSuccess() {
      cleanup();
      resolve();
    }

    function onError() {
      cleanup();
      reject(new Error(`Video decode error while waiting for ${eventName}`));
    }

    function cleanup() {
      clearTimeout(timer);
      target.removeEventListener(eventName, onSuccess);
      target.removeEventListener(errorEventName, onError);
    }

    target.addEventListener(eventName, onSuccess, { once: true });
    target.addEventListener(errorEventName, onError, { once: true });
  });
}

function canvasToJpegBlob(canvas, quality = 0.84) {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => {
        if (blob) {
          resolve(blob);
        } else {
          reject(new Error('Canvas failed to encode JPEG slide'));
        }
      },
      'image/jpeg',
      quality
    );
  });
}

/**
 * Extracts multi-window keyframes from a browser File using ONE reusable <canvas> (max width 960px),
 * applying bottom taskbar crop and pixel-delta filtering via URL.createObjectURL(file).
 */
export async function extractVideoKeyframes(file, options = {}) {
  const {
    layoutType = '2x2',
    taskbarCropPx = 48,
    maxFrames = 8,
    maxDurationSec = 300,
    deltaThreshold = 12,
    onProgress = null,
  } = options;

  const objectUrl = URL.createObjectURL(file);
  const video = document.createElement('video');
  video.muted = true;
  video.playsInline = true;
  video.preload = 'auto';
  video.src = objectUrl;

  // Single reusable offscreen <canvas> (max width 960px)
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d', { willReadFrequently: true });

  try {
    await waitForEvent(video, 'loadedmetadata');

    const duration = Number(video.duration || 0);
    if (!Number.isFinite(duration) || duration <= 0) {
      throw new Error('Could not read valid video duration.');
    }

    if (maxDurationSec && duration > maxDurationSec) {
      throw new ApiError(
        `Video is ${Math.round(duration / 60)} min long. Web Lite caps video previews at 5 minutes — download the System App for unlimited C++ OpenCV slicing.`,
        403,
        {
          error: 'EDITION_LIMIT_EXCEEDED',
          feature: 'video_duration',
          message: 'Videos longer than 5 minutes require the Downloaded System App.',
        }
      );
    }

    const videoW = Math.max(16, video.videoWidth || 1280);
    const cropBottom = Math.max(0, Math.min(200, Number(taskbarCropPx) || 0));
    const effectiveH = Math.max(16, (video.videoHeight || 720) - cropBottom);
    const rois = getLayoutRois(layoutType);

    const sampleSteps = Math.max(2, Math.min(12, maxFrames * 2));
    const prevSigs = new Map();
    const extractedSlides = [];

    for (let step = 0; step < sampleSteps && extractedSlides.length < maxFrames; step++) {
      const seekSec = Math.min(duration - 0.15, Math.max(0.2, (duration * (step + 0.5)) / sampleSteps));
      video.currentTime = seekSec;
      await waitForEvent(video, 'seeked');

      for (const roi of rois) {
        if (extractedSlides.length >= maxFrames) {
          break;
        }
        const sx = Math.floor((roi.xmin / 1000) * videoW);
        const sy = Math.floor((roi.ymin / 1000) * effectiveH);
        const sw = Math.max(16, Math.floor(((roi.xmax - roi.xmin) / 1000) * videoW));
        const sh = Math.max(16, Math.floor(((roi.ymax - roi.ymin) / 1000) * effectiveH));

        const targetW = Math.min(960, sw);
        const targetH = Math.max(16, Math.round((sh / sw) * targetW));
        if (canvas.width !== targetW) canvas.width = targetW;
        if (canvas.height !== targetH) canvas.height = targetH;

        ctx.drawImage(video, sx, sy, sw, sh, 0, 0, targetW, targetH);
        const imgData = ctx.getImageData(0, 0, targetW, targetH);
        const sig = sampleLuminanceSignature(imgData);
        const delta = computeSignatureDelta(prevSigs.get(roi.roi_key), sig);

        if (delta >= deltaThreshold) {
          prevSigs.set(roi.roi_key, sig);
          const blob = await canvasToJpegBlob(canvas, 0.84);
          const tsTag = Math.round(seekSec);
          extractedSlides.push({
            roi_key: roi.roi_key,
            roi_name: roi.name,
            timestamp_sec: tsTag,
            width: targetW,
            height: targetH,
            blob,
            filename: `${roi.roi_key}_t${tsTag}s.jpg`,
          });
        }
      }

      if (typeof onProgress === 'function') {
        onProgress(Math.round(((step + 1) / sampleSteps) * 100));
      }
    }

    return {
      duration,
      resolution: `${videoW}x${video.videoHeight || effectiveH}`,
      layoutType,
      rois,
      slides: extractedSlides,
    };
  } finally {
    video.removeAttribute('src');
    video.load();
    URL.revokeObjectURL(objectUrl);
  }
}
