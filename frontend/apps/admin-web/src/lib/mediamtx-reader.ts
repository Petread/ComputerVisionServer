const MEDIAMTX_READER_SCRIPT_URL = '/stream-webrtc/reader.js';
const MEDIAMTX_READER_SCRIPT_ID = 'mediamtx-webrtc-reader-v1-19-0';

export interface MediaMtxReaderConfiguration {
  url: string;
  user?: string;
  pass?: string;
  token?: string;
  onError?: (error: string) => void;
  onTrack?: (event: RTCTrackEvent) => void;
  onDataChannel?: (event: RTCDataChannelEvent) => void;
}

export interface MediaMtxReader {
  close(): void;
}

interface MediaMtxReaderConstructor {
  new (configuration: MediaMtxReaderConfiguration): MediaMtxReader;
}

declare global {
  interface Window {
    MediaMTXWebRTCReader?: MediaMtxReaderConstructor;
  }
}

let readerConstructorPromise: Promise<MediaMtxReaderConstructor> | null = null;

/** Load the reader.js shipped by the running, locally installed MediaMTX. */
export function loadMediaMtxReader(): Promise<MediaMtxReaderConstructor> {
  if (typeof window === 'undefined') {
    return Promise.reject(new Error('MediaMTX WebRTC reader requires a browser.'));
  }
  if (window.MediaMTXWebRTCReader) {
    return Promise.resolve(window.MediaMTXWebRTCReader);
  }
  if (readerConstructorPromise) {
    return readerConstructorPromise;
  }
  readerConstructorPromise = new Promise<MediaMtxReaderConstructor>((resolve, reject) => {
    const finish = () => {
      if (window.MediaMTXWebRTCReader) {
        resolve(window.MediaMTXWebRTCReader);
      } else {
        reject(new Error('MediaMTX reader.js loaded without registering its reader class.'));
      }
    };
    const fail = () => {
      reject(new Error('The local MediaMTX reader.js client could not be loaded.'));
    };
    const existing = document.getElementById(
      MEDIAMTX_READER_SCRIPT_ID
    ) as HTMLScriptElement | null;
    if (existing) {
      if (existing.dataset.mediamtxReaderLoaded === 'true') {
        finish();
        return;
      }
      existing.addEventListener('load', finish, { once: true });
      existing.addEventListener('error', fail, { once: true });
      return;
    }
    const script = document.createElement('script');
    script.id = MEDIAMTX_READER_SCRIPT_ID;
    script.src = MEDIAMTX_READER_SCRIPT_URL;
    script.async = true;
    script.addEventListener(
      'load',
      () => {
        script.dataset.mediamtxReaderLoaded = 'true';
        finish();
      },
      { once: true }
    );
    script.addEventListener('error', fail, { once: true });
    document.head.appendChild(script);
  }).catch((error: unknown) => {
    readerConstructorPromise = null;
    throw error;
  });
  return readerConstructorPromise;
}
