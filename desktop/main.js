// Promptloom's desktop app: a window for the built-in app, a menu bar / tray
// icon, and the daemon kept running. The Python package does everything
// else; this shell only talks to it over http://127.0.0.1:<port>.
'use strict';

const {app, BrowserWindow, Tray, Menu, nativeImage, shell, Notification, dialog} = require('electron');
const {spawn, execFileSync} = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');
const http = require('http');

const PORT = Number(process.env.PROMPTLOOM_PORT || 8765);
const BASE = `http://127.0.0.1:${PORT}/`;
const IS_MAC = process.platform === 'darwin';
const IS_WIN = process.platform === 'win32';

let win = null;
let tray = null;
let daemon = null;
let status = {};
let quitting = false;

// -- the promptloom command ---------------------------------------------------

function candidates() {
  const home = os.homedir();
  const names = IS_WIN ? ['promptloom.exe', 'promptloom'] : ['promptloom'];
  const dirs = [
    path.join(home, '.local', 'bin'),
    '/usr/local/bin', '/opt/homebrew/bin', '/usr/bin',
    path.join(home, 'AppData', 'Roaming', 'Python', 'Scripts'),
    ...(process.env.PATH || '').split(path.delimiter),
  ];
  const out = [];
  for (const dir of dirs) for (const name of names) out.push(path.join(dir, name));
  return out;
}

function findPromptloom() {
  for (const candidate of candidates()) {
    try { fs.accessSync(candidate, fs.constants.X_OK); return candidate; } catch { /* next */ }
  }
  return null;
}

function get(pathname) {
  return new Promise((resolve, reject) => {
    const req = http.get(BASE + pathname, {timeout: 2000}, res => {
      let body = '';
      res.on('data', d => { body += d; });
      res.on('end', () => {
        if (res.statusCode !== 200) return reject(new Error(`HTTP ${res.statusCode}`));
        try { resolve(JSON.parse(body)); } catch (e) { reject(e); }
      });
    });
    req.on('error', reject);
    req.on('timeout', () => req.destroy(new Error('timeout')));
  });
}

async function daemonUp() {
  try { await get('api/status'); return true; } catch { return false; }
}

// Start the daemon when nothing is serving. The installed service (systemd,
// launchd, the scheduled task) normally does this; this covers a bare install.
async function ensureDaemon() {
  if (await daemonUp()) return true;
  const binary = findPromptloom();
  if (!binary) return false;
  daemon = spawn(binary, ['watch'], {stdio: 'ignore', detached: !IS_WIN, windowsHide: true});
  daemon.on('exit', () => { daemon = null; });
  for (let i = 0; i < 40; i++) {
    await new Promise(r => setTimeout(r, 250));
    if (await daemonUp()) return true;
  }
  return false;
}

function runPromptloom(args) {
  const binary = findPromptloom();
  if (!binary) return;
  spawn(binary, args, {stdio: 'ignore', detached: !IS_WIN, windowsHide: true}).unref();
}

// -- the window ----------------------------------------------------------------

function iconPath(name) { return path.join(__dirname, 'build', name); }

function createWindow(page = '') {
  if (win) {
    if (page) win.loadURL(BASE + '#/' + page);
    if (win.isMinimized()) win.restore();
    win.show();
    win.focus();
    return win;
  }
  win = new BrowserWindow({
    width: 1280, height: 860, minWidth: 720, minHeight: 480,
    title: 'Promptloom',
    icon: IS_MAC ? undefined : iconPath('icon.png'),
    titleBarStyle: IS_MAC ? 'hiddenInset' : 'default',
    trafficLightPosition: {x: 14, y: 14},
    backgroundColor: '#121211',
    show: false,
    webPreferences: {preload: path.join(__dirname, 'preload.js'), contextIsolation: true, sandbox: true},
  });
  win.once('ready-to-show', () => win.show());
  win.loadURL(BASE + (page ? '#/' + page : ''));
  // Links to other sites open in the browser; the app stays on its own pages.
  win.webContents.setWindowOpenHandler(({url}) => { shell.openExternal(url); return {action: 'deny'}; });
  win.webContents.on('will-navigate', (e, url) => { if (!url.startsWith(BASE)) { e.preventDefault(); shell.openExternal(url); } });
  // Closing the window keeps the app in the menu bar / tray, like Slack or Claude.
  win.on('close', e => { if (!quitting) { e.preventDefault(); win.hide(); if (IS_MAC) app.dock?.hide(); } });
  win.on('show', () => { if (IS_MAC) app.dock?.show(); });
  return win;
}

function showSetup() {
  const w = new BrowserWindow({width: 640, height: 420, title: 'Promptloom', resizable: false,
    webPreferences: {contextIsolation: true, sandbox: true}});
  w.loadFile(path.join(__dirname, 'setup.html'));
}

// -- the menu bar / tray icon ----------------------------------------------------

function trayImage() {
  if (IS_MAC) {
    const img = nativeImage.createFromPath(iconPath('trayTemplate.png'));
    img.setTemplateImage(true);  // macOS tints it for light and dark menu bars
    return img;
  }
  return nativeImage.createFromPath(iconPath(IS_WIN ? 'tray-win.png' : 'tray.png'));
}

function needs() {
  const prs = status.prs || {};
  return (prs.waiting || 0) + (prs.asked || 0);
}

function trayMenu() {
  const active = status.active;
  const items = [];
  if (active) {
    items.push({label: `Now: ${active.title.slice(0, 48)}`, click: () => createWindow('session/' + encodeURIComponent(active.key))});
    items.push({type: 'separator'});
  }
  items.push(
    {label: 'New prompt…', accelerator: 'CmdOrCtrl+N', click: () => createWindow('compose')},
    {label: 'Open Promptloom', click: () => createWindow('')},
    {label: 'Search prompts', click: () => createWindow('search')},
    {label: 'Stream', click: () => createWindow('stream')},
    {label: 'Threads', click: () => createWindow('threads')},
  );
  if (status.prs?.enabled) items.push({label: needs() ? `Pull requests (${needs()} need you)` : 'Pull requests', click: () => createWindow('prs')});
  const recent = (status.recent || []).filter(s => !active || s.key !== active.key).slice(0, 6);
  if (recent.length) {
    items.push({type: 'separator'});
    for (const s of recent) items.push({label: s.title.slice(0, 48), click: () => createWindow('session/' + encodeURIComponent(s.key))});
  }
  if (status.vault) {
    items.push({type: 'separator'});
    items.push({label: 'Open in Obsidian', submenu: [
      {label: 'Vault home', click: () => runPromptloom(['open', 'home'])},
      {label: 'Connections map', click: () => runPromptloom(['open', 'map'])},
      {label: 'Action items', click: () => runPromptloom(['open', 'actions'])},
    ]});
  }
  items.push({type: 'separator'});
  if (status.waiting) items.push({label: `Summaries paused: ${status.waiting}`.slice(0, 70), enabled: false});
  items.push({label: 'Quit Promptloom', accelerator: 'CmdOrCtrl+Q', click: () => { quitting = true; app.quit(); }});
  return Menu.buildFromTemplate(items);
}

function createTray() {
  tray = new Tray(trayImage());
  tray.setToolTip('Promptloom');
  refreshTray();
  tray.on('click', () => { if (!IS_MAC) createWindow(''); });
  if (IS_MAC) tray.on('right-click', () => tray.popUpContextMenu());
}

function refreshTray() {
  if (!tray) return;
  tray.setContextMenu(trayMenu());
  const active = status.active;
  tray.setToolTip(active ? `Promptloom: ${active.title}` : 'Promptloom');
  if (IS_MAC) tray.setTitle(needs() ? String(needs()) : '');
  if (!IS_MAC && app.setBadgeCount) app.setBadgeCount(needs());
}

let lastNotified = '';
async function poll() {
  try {
    const fresh = await get('api/status');
    const prs = fresh.prs || {};
    const key = `${prs.waiting || 0}/${prs.asked || 0}`;
    if (lastNotified && key !== lastNotified && (prs.waiting || prs.asked) && Notification.isSupported()) {
      const n = new Notification({title: 'Promptloom', body: `${(prs.waiting || 0) + (prs.asked || 0)} pull request${(prs.waiting || 0) + (prs.asked || 0) > 1 ? 's' : ''} need you`});
      n.on('click', () => createWindow('prs'));
      n.show();
    }
    lastNotified = key;
    status = fresh;
  } catch {
    status = {...status, waiting: 'the promptloom daemon is not running'};
  }
  refreshTray();
}

// -- app menu (macOS needs one for copy and paste to work) -----------------------

function appMenu() {
  const template = [
    ...(IS_MAC ? [{role: 'appMenu'}] : []),
    {label: 'File', submenu: [
      {label: 'New prompt', accelerator: 'CmdOrCtrl+N', click: () => createWindow('compose')},
      {label: 'Search', accelerator: 'CmdOrCtrl+F', click: () => createWindow('search')},
      {type: 'separator'},
      IS_MAC ? {role: 'close'} : {role: 'quit'},
    ]},
    {role: 'editMenu'},
    {label: 'View', submenu: [{role: 'reload'}, {role: 'toggleDevTools'}, {type: 'separator'}, {role: 'resetZoom'}, {role: 'zoomIn'}, {role: 'zoomOut'}, {type: 'separator'}, {role: 'togglefullscreen'}]},
    {role: 'windowMenu'},
    {role: 'help', submenu: [{label: 'Promptloom on GitHub', click: () => shell.openExternal('https://github.com/craigcampbell/promptloom')}]},
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// -- lifecycle -------------------------------------------------------------------

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', (_e, argv) => {
    const page = argv.find(a => a.startsWith('promptloom://'))?.replace('promptloom://', '') || '';
    createWindow(page);
  });
  app.setAsDefaultProtocolClient('promptloom');
  app.on('open-url', (e, url) => { e.preventDefault(); createWindow(url.replace('promptloom://', '')); });

  app.whenReady().then(async () => {
    if (IS_WIN) app.setAppUserModelId('io.github.craigcampbell.promptloom');
    appMenu();
    const up = await ensureDaemon();
    if (!up) { showSetup(); return; }
    await poll();
    createTray();
    setInterval(poll, 10000);
    const page = process.argv.find(a => a.startsWith('promptloom://'))?.replace('promptloom://', '')
      || process.argv.slice(1).find(a => !a.startsWith('-')) || '';
    createWindow(page);
  });

  app.on('activate', () => createWindow(''));  // the dock icon on macOS
  app.on('window-all-closed', () => { /* stay in the menu bar / tray */ });
  app.on('before-quit', () => { quitting = true; });
}
