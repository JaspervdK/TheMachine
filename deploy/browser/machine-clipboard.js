// The Machine: your own clipboard in your server's browser. Ctrl+C / Ctrl+V (Cmd on a Mac) copy and paste
// between your computer and that browser; noVNC's side panel clipboard stays for phones.
import UI from './app/ui.js';

const CONTROL = 0xffe3, KEYS = {a: 0x61, c: 0x63, v: 0x76, x: 0x78, z: 0x7a};
const mac = /Mac|iPhone|iPad/.test(navigator.platform);
let hooked = null, swallow = null;

setInterval(() => {  // noVNC makes a new connection object on every (re)connect
    if (!UI.rfb || UI.rfb === hooked) return;
    hooked = UI.rfb;
    hooked.addEventListener('clipboard', event => navigator.clipboard?.writeText(event.detail.text).catch(() => {}));
}, 300);

function combo(rfb, key) {
    if (mac) {  // Cmd is Super over there; the browser there copies and pastes with Ctrl
        rfb.sendKey(0xffeb, 'MetaLeft', false);
        rfb.sendKey(0xffe7, 'MetaLeft', false);
    }
    rfb.sendKey(CONTROL, 'ControlLeft', true);
    rfb.sendKey(KEYS[key], 'Key' + key.toUpperCase(), true);
    rfb.sendKey(KEYS[key], 'Key' + key.toUpperCase(), false);
    if (mac) rfb.sendKey(CONTROL, 'ControlLeft', false);
}

window.addEventListener('keydown', event => {
    const key = event.key.toLowerCase(), screen = document.getElementById('noVNC_container');
    if (!UI.rfb || !screen?.contains(event.target) || !(mac ? event.metaKey : event.ctrlKey) || event.altKey || !(key in KEYS)) return;
    if (!mac && key !== 'v') return;  // Ctrl already means Ctrl there; only pasting needs your clipboard first
    event.preventDefault();
    event.stopImmediatePropagation();
    swallow = event.code;
    const rfb = UI.rfb;
    if (key !== 'v') return combo(rfb, key);
    (navigator.clipboard?.readText() ?? Promise.reject())
        .then(text => rfb.clipboardPasteFrom(text), () => {})
        .finally(() => setTimeout(() => combo(rfb, 'v'), 50));
}, true);

window.addEventListener('keyup', event => {
    if (event.code === swallow) {
        swallow = null;
        event.preventDefault();
        event.stopImmediatePropagation();
    }
}, true);
