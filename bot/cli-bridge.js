console.log("[Startup] Starting bot...");
require('dotenv').config();
const { Telegraf, Markup } = require('telegraf');
const { spawn, exec } = require('child_process');
const fs = require('fs');
const fsp = fs.promises;
const axios = require('axios');
const path = require('path');

const ALLOWED_ID = process.env.ALLOWED_ID;
const bot = new Telegraf(process.env.TELEGRAM_BOT_TOKEN);

const logger = {
    info:  (m)    => console.log(`[INFO]  [${new Date().toLocaleTimeString()}] ${m}`),
    error: (m, e) => console.error(`[ERROR] [${new Date().toLocaleTimeString()}] ${m}`, e || ''),
    debug: (m)    => console.log(`[DEBUG] [${new Date().toLocaleTimeString()}] ${m}`)
};

// --- PATHS ---
// Python video-edit skill lives one level up, in ../video-edit
const SKILL_DIR     = process.env.SKILL_DIR || path.join(__dirname, '..', 'video-edit');
const TRACKS_DIR    = path.join(SKILL_DIR, 'tracks');
const RENDER_LOG    = path.join(SKILL_DIR, 'cache', 'render_log.json');
const ACTIVE_STATE_PATH = path.join(SKILL_DIR, 'active_state.json');
const REFS_DIR      = process.env.REFS_DIR    || path.join(SKILL_DIR, 'refs');
const OUT_DIR       = process.env.OUT_DIR     || path.join(SKILL_DIR, 'output');
const PYTHON_EXE    = process.env.PYTHON_EXE  || 'C:\\Users\\user\\AppData\\Local\\Programs\\Python\\Python312\\python.exe';
const YTDLP_EXE     = process.env.YTDLP_EXE   || 'C:\\Users\\user\\AppData\\Local\\Programs\\Python\\Python312\\Scripts\\yt-dlp.exe';
const MAX_CONCURRENT_RENDERS = parseInt(process.env.MAX_CONCURRENT_RENDERS || '2', 10);
const FSM_TTL_MS    = 5 * 60 * 1000;
const STDERR_CAP    = 64 * 1024;

// Поддерживаемые платформы для скачивания референсов через yt-dlp.
// Регулярка ловит ПЕРВУЮ подходящую ссылку в сообщении.
const REF_URL_RE = /https?:\/\/(?:www\.|m\.|vm\.)?(?:instagram\.com\/(?:reel|reels|p|tv|share)\/[^\s]+|tiktok\.com\/[^\s]+|vm\.tiktok\.com\/[^\s]+|youtube\.com\/(?:watch\?v=|shorts\/|embed\/)[^\s]+|youtu\.be\/[^\s]+|x\.com\/[^\s]+\/status\/\d+|twitter\.com\/[^\s]+\/status\/\d+)/i;

// Очередь скачиваний — последовательно, чтоб не дудосить yt-dlp и не упереться в rate-limit.
const downloadQueue = [];
let downloadBusy = false;
async function pushDownload(fn) {
    downloadQueue.push(fn);
    if (downloadBusy) return;
    downloadBusy = true;
    try {
        while (downloadQueue.length) {
            const job = downloadQueue.shift();
            try { await job(); } catch (e) { logger.error('download job fail', e); }
        }
    } finally { downloadBusy = false; }
}

let activeRenders = 0;
const drafts = new Map();
const trackFSM = new Map();
const activeProcesses = new Map();
let tracksCache = [];

// --- LOOKS & FONTS ---
const LOOKS = {
    'original':     '🎬 Original',
    'neo_noir':     '🎬 Neo-Noir',
    'bw_classic':   '⚫ BW classic',
    'bw_contrast':  '⚫ BW contrast',
    'bw_soft':      '⚫ BW soft',
    'bw_grain':     '⚫ BW grain',
    'color_punchy': '🎨 Color punchy',
    'color_filmic': '🎨 Color filmic',
    'teal_orange':  '🎨 Teal/Orange',
};
const FONTS = {
    'engineer':   '⚙ Engineer',
    'mono':       '⌨ Mono',
    'geometric':  '📐 Geometric',
    'gothic':     '🅰 Gothic',
    'minimal':    '✨ Minimal',
    'condensed':  '▌ Condensed',
};
const FONT_MAP = {
    'engineer':  'PF DinDisplay Pro',
    'mono':      'JetBrains Mono',
    'geometric': 'Futura PT',
    'gothic':    'League Gothic',
    'minimal':   'Montserrat',
    'condensed': 'Archivo Narrow',
};
const CAPTION_ANIMS = {
    'none':      '❌ ВЫКЛ',
    'reel_boss': '🔥 Reel Boss',
    'bounce':    '🎈 Bounce',
    'shake':     '📳 Shake',
    'glitch':    '👾 Glitch',
    'jump':      '🦘 Jump',
    'flicker':   '✨ Flicker',
};
const SIZES = [30, 40, 50, 60, 70, 90];

// --- SAFETY ---
function sanitizeFilename(raw) {
    // Whitelist letters/digits/_/-, кириллица сохраняется
    let s = String(raw || '').normalize('NFKD');
    s = s.replace(/\.[^/.]+$/, '');           // strip extension
    s = s.replace(/[^a-zA-Z0-9_\-а-яёА-ЯЁ]/g, '_');
    s = s.replace(/_+/g, '_').replace(/^_+|_+$/g, '');
    s = s.toLowerCase();
    if (!s || s === '.' || s === '..') return null;
    return s.slice(0, 80);
}

function isInsideDir(parent, child) {
    const p = path.resolve(parent);
    const c = path.resolve(child);
    return c === p || c.startsWith(p + path.sep);
}

function isAuthorized(ctx) {
    return ctx.from && ctx.from.id.toString() === ALLOWED_ID;
}

// --- YT-DLP REFERENCE DOWNLOADER ---
function runYtDlp(url, outDir) {
    return new Promise((resolve) => {
        const outTpl = path.join(outDir, 'ytdlp_%(id)s.%(ext)s');
        const args = [
            '--no-playlist',
            '--no-warnings',
            '--no-overwrites',
            '--restrict-filenames',
            '-f', 'bv*+ba/b',
            '--merge-output-format', 'mp4',
            '-o', outTpl,
            '--print', 'after_move:filepath',
            url,
        ];
        const child = spawn(YTDLP_EXE, args, { windowsHide: true });
        let out = '', err = '';
        child.stdout.on('data', d => { out += d.toString(); });
        child.stderr.on('data', d => { err += d.toString(); });
        const timer = setTimeout(() => { try { child.kill(); } catch (_) {} }, 180_000);
        child.on('close', (code) => {
            clearTimeout(timer);
            let filePath = out.trim().split(/\r?\n/).filter(Boolean).pop();
            let alreadyExisted = false;
            if ((!filePath || !fs.existsSync(filePath)) && code === 0) {
                const m = (out + '\n' + err).match(/(\S+\.\w{2,4})\s+has already been (?:downloaded|recorded)/i);
                if (m && fs.existsSync(m[1])) {
                    filePath = m[1];
                    alreadyExisted = true;
                }
            }
            const ok = code === 0 && filePath && fs.existsSync(filePath);
            resolve({ ok, code, filePath, err, alreadyExisted });
        });
        child.on('error', (e) => { clearTimeout(timer); resolve({ ok: false, code: -1, err: e.message }); });
    });
}

async function countRefs() {
    try {
        const entries = await fsp.readdir(REFS_DIR);
        const exts = new Set(['.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v']);
        return entries.filter(f => exts.has(path.extname(f).toLowerCase())).length;
    } catch { return 0; }
}

async function handleRefUrl(ctx, url) {
    const chatId = ctx.chat.id;
    if (!fs.existsSync(REFS_DIR)) {
        try { await fsp.mkdir(REFS_DIR, { recursive: true }); }
        catch (e) {
            await ctx.reply(`❌ Папка рефов недоступна: ${REFS_DIR}`).catch(() => {});
            return;
        }
    }
    logger.info(`DOWNLOAD_STARTED url=${url}`);
    const status = await ctx.reply('⏳ Тяну реф в макс качестве...').catch(() => null);
    try {
        const r = await runYtDlp(url, REFS_DIR);
        if (status) await ctx.telegram.deleteMessage(chatId, status.message_id).catch(() => {});
        if (!r.ok) {
            const tail = (r.err || '').trim().split('\n').slice(-3).join('\n').slice(-500);
            logger.error(`DOWNLOAD_FAILED rc=${r.code} ${tail}`);
            await ctx.reply(`❌ yt-dlp упал (rc=${r.code}). ${tail}`).catch(() => {});
            return;
        }
        const stat = fs.statSync(r.filePath);
        const sizeMb = (stat.size / 1024 / 1024).toFixed(1);
        const total = await countRefs();
        const prefix = r.alreadyExisted ? '♻️ Уже был' : '✅';
        logger.info(`DOWNLOAD_FINISHED ${path.basename(r.filePath)} ${sizeMb}MB total=${total}`);
        await ctx.reply(
            `${prefix} ${path.basename(r.filePath)} • ${sizeMb} MB → рефы\nВсего: ${total}`
        ).catch(() => {});
    } catch (e) {
        if (status) await ctx.telegram.deleteMessage(chatId, status.message_id).catch(() => {});
        logger.error('handleRefUrl error', e);
        await ctx.reply(`❌ Download error: ${e.message}`).catch(() => {});
    }
}

// --- CORE UTILS ---
async function loadTracks() {
    try {
        if (!fs.existsSync(TRACKS_DIR)) await fsp.mkdir(TRACKS_DIR, { recursive: true }).catch(() => {});
        const dirs = await fsp.readdir(TRACKS_DIR);
        const list = [];
        for (const dir of dirs) {
            const trackPath = path.join(TRACKS_DIR, dir);
            const metaPath = path.join(trackPath, 'metadata.json');
            try {
                await fsp.access(metaPath);
                const meta = JSON.parse(await fsp.readFile(metaPath, 'utf8'));
                // backfill missing fields
                if (meta.lyrics_start_time === undefined) meta.lyrics_start_time = 0;
                if (meta.subtitle_start_line === undefined) meta.subtitle_start_line = 1;
                list.push({ id: dir, name: meta.name || dir, path: trackPath, meta });
            } catch (e) {
                if (e.code !== 'ENOENT') logger.error(`Tracks meta fail for ${dir}`, e);
            }
        }
        tracksCache = list;
        return list;
    } catch (e) { logger.error('loadTracks fail', e); return []; }
}

let metaQueue = Promise.resolve();
function updateTrackMeta(trackId, update) {
    metaQueue = metaQueue.then(async () => {
        const track = tracksCache.find(t => t.id === trackId);
        if (!track) return;
        track.meta = { ...track.meta, ...update };
        if (update.name) track.name = update.name;
        const metaPath = path.join(track.path, 'metadata.json');
        await atomicWrite(metaPath, track.meta);
    }).catch(e => logger.error('updateTrackMeta queue', e));
    return metaQueue;
}

async function getActiveState() {
    try {
        await fsp.access(ACTIVE_STATE_PATH);
        return JSON.parse(await fsp.readFile(ACTIVE_STATE_PATH, 'utf8'));
    } catch (e) {}
    const tracks = await loadTracks();
    const def = tracks.find(t => t.meta?.default) || tracks[0] || null;
    return { track_id: def ? def.id : null };
}

async function atomicWrite(filePath, data) {
    const lockPath = filePath + '.lock';
    let locked = false;
    for (let i = 0; i < 20; i++) {
        try {
            await fsp.mkdir(lockPath);
            await fsp.writeFile(path.join(lockPath, 'pid'), String(process.pid)).catch(() => {});
            locked = true;
            break;
        } catch (e) {
            await new Promise(r => setTimeout(r, 50));
        }
    }
    try {
        const tmpPath = filePath + '.tmp';
        await fsp.writeFile(tmpPath, JSON.stringify(data, null, 2), 'utf8');
        await fsp.rename(tmpPath, filePath);
    } catch (e) {
        logger.error(`atomicWrite failed for ${filePath}`, e);
        throw e;
    } finally {
        if (locked) {
            await fsp.rm(lockPath, { recursive: true, force: true }).catch(() => {});
        }
    }
}

async function saveActiveState(update) {
    const state = { ...(await getActiveState()), ...update };
    await atomicWrite(ACTIVE_STATE_PATH, state);
}

async function getNextEditNum() {
    try {
        const log = JSON.parse(await fsp.readFile(RENDER_LOG, 'utf8'));
        return Array.isArray(log) ? log.length + 1 : 1;
    } catch { return 1; }
}

function setFSM(chatId, state) {
    const stamp = Date.now();
    trackFSM.set(chatId, { ...state, ts: stamp });
    setTimeout(() => {
        const cur = trackFSM.get(chatId);
        if (cur && cur.ts === stamp) {
            trackFSM.delete(chatId);
            logger.debug(`FSM TTL expired chat=${chatId}`);
        }
    }, FSM_TTL_MS);
}

function killTree(pid) {
    if (!pid) return;
    if (process.platform === 'win32') {
        exec(`taskkill /PID ${pid} /T /F`, () => {});
    } else {
        try { process.kill(-pid, 'SIGTERM'); } catch (_) {}
    }
}

// --- KEYBOARDS ---
function buildDraftKeyboard(env) {
    const k = (label, op) => Markup.button.callback(label, `d:${op}`);
    const reuse = env.EXCLUDE_USED === '0';
    const modeLabel  = env.DISPLAY_MODE === 'word' ? '📐 Режим: СЛОВО' : '📝 Режим: ФРАЗА';
    const frameLabel = env.FRAME_MODE === 'original' ? '🎥 Frame: ORIGINAL' : '📱 Frame: 9:16';
    return Markup.inlineKeyboard([
        [k(`🎵 Трек: ${tracksCache.find(t => t.id === env.TRACK_ID)?.name || 'Выбрать'}`, 'm_track')],
        [k('🎬 Style', 'm_look'), k('🔡 Шрифт', 'm_font')],
        [k('✨ Текст-FX', 'm_anim'), k(env.SHAKE === '1' ? '📳 Тряска: ВКЛ' : '📳 Тряска: ВЫКЛ', 'tog_shake')],
        [k(modeLabel, 'tog_mode'), k(frameLabel, 'tog_frame')],
        [k('☀️ Свет', 'm_exp'), k('🅰 Размер', 'm_size')],
        [k('⏱ −5s', 'dur-'), k('⏱ +5s', 'dur+'), k('▶ +20s', 'off+')],
        [k(reuse ? '🔄 Реюз: ВКЛ' : '🔄 Реюз: ВЫКЛ', 'tog_reuse')],
        [k(env.CAPTIONS === 'none' ? '🔇 Без сабов' : '📝 С сабами', 'nocap')],
        [k('🚀 ЗАПУСТИТЬ РЕНДЕР', 'render')]
    ]);
}

function buildTrackKeyboard() {
    const rows = tracksCache.map(t => [Markup.button.callback(`🎵 ${t.name}`, `d:tr_${t.id}`.slice(0, 60))]);
    rows.push([Markup.button.callback('⬅ Назад', 'd:back')]);
    return Markup.inlineKeyboard(rows);
}

function buildTrackListKeyboard(tracks) {
    const rows = tracks.map(t => [Markup.button.callback(`🎵 ${t.name}`, `admin:view_${t.id}`.slice(0, 60))]);
    rows.push([Markup.button.callback('⬅ Назад', 'admin:back')]);
    return Markup.inlineKeyboard(rows);
}

function buildTrackCardKeyboard(trackId) {
    return Markup.inlineKeyboard([
        [Markup.button.callback('✏ Изменить начало текста', `admin:edit_offset_${trackId}`.slice(0, 60))],
        [Markup.button.callback('✏ Изменить строку субтитров', `admin:edit_subline_${trackId}`.slice(0, 60))],
        [Markup.button.callback('✏ Переименовать', `admin:rename_${trackId}`.slice(0, 60))],
        [Markup.button.callback('🗑 Удалить', `admin:delete_${trackId}`.slice(0, 60))],
        [Markup.button.callback('⬅ Назад', 'admin:list')]
    ]);
}

function buildLookKeyboard() {
    const rows = [], keys = Object.keys(LOOKS);
    for (let i = 0; i < keys.length; i += 2) {
        const row = [Markup.button.callback(LOOKS[keys[i]], `d:l_${keys[i]}`)];
        if (keys[i + 1]) row.push(Markup.button.callback(LOOKS[keys[i + 1]], `d:l_${keys[i + 1]}`));
        rows.push(row);
    }
    rows.push([Markup.button.callback('⬅ Назад', 'd:back')]);
    return Markup.inlineKeyboard(rows);
}

function buildFontKeyboard() {
    const rows = [], keys = Object.keys(FONTS);
    for (let i = 0; i < keys.length; i += 2) {
        const row = [Markup.button.callback(FONTS[keys[i]], `d:f_${keys[i]}`)];
        if (keys[i + 1]) row.push(Markup.button.callback(FONTS[keys[i + 1]], `d:f_${keys[i + 1]}`));
        rows.push(row);
    }
    rows.push([Markup.button.callback('⬅ Назад', 'd:back')]);
    return Markup.inlineKeyboard(rows);
}

function buildExposureKeyboard() {
    return Markup.inlineKeyboard([
        [Markup.button.callback('🌑 -0.6', 'd:e_-6'), Markup.button.callback('🌑 -0.3', 'd:e_-3'), Markup.button.callback('☀️ 0', 'd:e_0')],
        [Markup.button.callback('☀️ +0.3', 'd:e_+3'), Markup.button.callback('☀️ +0.6', 'd:e_+6')],
        [Markup.button.callback('⬅ Назад', 'd:back')],
    ]);
}

function buildSizeKeyboard() {
    const rows = [];
    for (let i = 0; i < SIZES.length; i += 3) {
        const row = SIZES.slice(i, i + 3).map(s => Markup.button.callback(`🅰 ${s}px`, `d:s_${s}`));
        rows.push(row);
    }
    rows.push([Markup.button.callback('⬅ Назад', 'd:back')]);
    return Markup.inlineKeyboard(rows);
}

function buildAnimKeyboard() {
    const rows = [], keys = Object.keys(CAPTION_ANIMS);
    for (let i = 0; i < keys.length; i += 2) {
        const row = [Markup.button.callback(CAPTION_ANIMS[keys[i]], `d:a_${keys[i]}`)];
        if (keys[i + 1]) row.push(Markup.button.callback(CAPTION_ANIMS[keys[i + 1]], `d:a_${keys[i + 1]}`));
        rows.push(row);
    }
    rows.push([Markup.button.callback('⬅ Назад', 'd:back')]);
    return Markup.inlineKeyboard(rows);
}

function buildAdminMenu() {
    return Markup.inlineKeyboard([
        [Markup.button.callback('➕ Добавить трек', 'admin:add')],
        [Markup.button.callback('📋 Список треков', 'admin:list')],
        [Markup.button.callback('⬅ Назад', 'admin:back_main')]
    ]);
}

function defaultDraftEnv(trackId) {
    return {
        LOOK: 'neo_noir',
        FONT_FAMILY: 'engineer',
        FONT_SIZE: '50',
        CAPTIONS_ANIM: 'bounce',
        SHAKE: '0',
        DISPLAY_MODE: 'word',
        FRAME_MODE: 'vertical',
        CAPTIONS: 'auto',
        DURATION: '25',
        OFFSET: '22',
        EXPOSURE: '0.0',
        EXCLUDE_USED: '1',
        TRACK_ID: trackId
    };
}

function editMsgCatch(e) {
    if (e && /not modified/i.test(String(e.description || e.message || ''))) return;
    logger.error('editMessage fail', e);
}

// --- MENU ACTIONS ---
async function sendDraftControls(ctx) {
    try {
        const chatId = ctx.chat.id;
        let env = drafts.get(chatId);
        const state = await getActiveState();
        if (!env) {
            env = defaultDraftEnv(state.track_id);
            drafts.set(chatId, env);
        }

        const track = tracksCache.find(t => t.id === env.TRACK_ID);
        // sync OFFSET from track meta only on first open (do not override if user already +20'd)
        if (track && !env._offset_synced) {
            env.OFFSET = String(track.meta.lyrics_start_time || 0);
            env._offset_synced = true;
        }

        const font  = FONTS[env.FONT_FAMILY] || env.FONT_FAMILY;
        const anim  = CAPTION_ANIMS[env.CAPTIONS_ANIM] || env.CAPTIONS_ANIM;
        const reuse = env.EXCLUDE_USED === '0';

        const lines = [
            `🏗 **КОНСТРУКТОР ЭДИТА**`,
            `🎵 **Трек:** ${track ? track.name : 'Не выбран'}`,
            `🎬 **Style:** ${LOOKS[env.LOOK] || env.LOOK}`,
            `🔡 **Font:** ${font} (${env.FONT_SIZE}px)`,
            `✨ **FX:** ${env.CAPTIONS === 'none' ? '❌ ВЫКЛ' : anim}`,
            `📳 **Shake:** ${env.SHAKE === '1' ? 'ВКЛ' : 'ВЫКЛ'}`,
            `📐 **Layout:** ${env.DISPLAY_MODE === 'phrase' ? 'ФРАЗА' : 'СЛОВО'}`,
            `🖼 **Frame:** ${env.FRAME_MODE === 'original' ? 'ORIGINAL' : '9:16'}`,
            `☀️ **Exposure:** ${env.EXPOSURE}`,
            `⏱ **Duration:** ${env.DURATION}s  **Offset:** ${env.OFFSET}s`,
            `🔄 **Clips:** ${reuse ? 'РЕЮЗ' : 'СВЕЖИЕ'}`,
        ];

        const kb = buildDraftKeyboard(env);
        if (ctx.callbackQuery) {
            await ctx.editMessageText(lines.join('\n'), { parse_mode: 'Markdown', reply_markup: kb.reply_markup }).catch(editMsgCatch);
        } else {
            await ctx.reply(lines.join('\n'), { parse_mode: 'Markdown', reply_markup: kb.reply_markup }).catch(editMsgCatch);
        }
    } catch (e) { logger.error('sendDraftControls fail', e); }
}

async function sendTrackCard(ctx, trackId) {
    const track = tracksCache.find(t => t.id === trackId);
    if (!track) return ctx.reply('❌ Трек не найден.').catch(() => {});

    const lines = [
        `🎵 **${track.name}**`,
        `Длительность: ${track.meta.duration || '?'} сек`,
        `Начало текста: ${track.meta.lyrics_start_time || 0} сек`,
        `Строка начала субтитров: ${track.meta.subtitle_start_line || 1}`
    ];

    const opts = {
        parse_mode: 'Markdown',
        reply_markup: buildTrackCardKeyboard(trackId).reply_markup
    };

    if (ctx.callbackQuery) {
        await ctx.editMessageText(lines.join('\n'), opts).catch(editMsgCatch);
    } else {
        await ctx.reply(lines.join('\n'), opts).catch(editMsgCatch);
    }
}

// --- HANDLERS ---
async function handleDraftCallback(ctx, op) {
    const start = Date.now();
    logger.info(`BUTTON_CLICK user=${ctx.from.id} callback=d:${op}`);
    await ctx.answerCbQuery().catch(() => {});

    try {
        const chatId = ctx.chat.id;
        let env = drafts.get(chatId);
        if (!env) {
            const state = await getActiveState();
            env = defaultDraftEnv(state.track_id);
            drafts.set(chatId, env);
        }

        if (op === 'm_track')      await ctx.editMessageReplyMarkup(buildTrackKeyboard().reply_markup).catch(editMsgCatch);
        else if (op === 'm_look')  await ctx.editMessageReplyMarkup(buildLookKeyboard().reply_markup).catch(editMsgCatch);
        else if (op === 'm_font')  await ctx.editMessageReplyMarkup(buildFontKeyboard().reply_markup).catch(editMsgCatch);
        else if (op === 'm_exp')   await ctx.editMessageReplyMarkup(buildExposureKeyboard().reply_markup).catch(editMsgCatch);
        else if (op === 'm_size')  await ctx.editMessageReplyMarkup(buildSizeKeyboard().reply_markup).catch(editMsgCatch);
        else if (op === 'm_anim')  await ctx.editMessageReplyMarkup(buildAnimKeyboard().reply_markup).catch(editMsgCatch);
        else if (op === 'back')    await sendDraftControls(ctx);
        else if (op.startsWith('tr_')) {
            env.TRACK_ID = op.slice(3);
            env._offset_synced = false;
            await saveActiveState({ track_id: env.TRACK_ID });
            await sendDraftControls(ctx);
        }
        else if (op.startsWith('l_'))  { env.LOOK = op.slice(2);          await sendDraftControls(ctx); }
        else if (op.startsWith('f_'))  { env.FONT_FAMILY = op.slice(2);   await sendDraftControls(ctx); }
        else if (op.startsWith('a_'))  { env.CAPTIONS_ANIM = op.slice(2); await sendDraftControls(ctx); }
        else if (op.startsWith('s_'))  { env.FONT_SIZE = op.slice(2);     await sendDraftControls(ctx); }
        else if (op.startsWith('e_'))  {
            const v = op.slice(2);
            const num = parseInt(v, 10) / 10;
            env.EXPOSURE = (isNaN(num) ? 0.0 : num).toFixed(1);
            await sendDraftControls(ctx);
        }
        else if (op === 'tog_shake') { env.SHAKE        = env.SHAKE === '1' ? '0' : '1'; await sendDraftControls(ctx); }
        else if (op === 'tog_mode')  { env.DISPLAY_MODE = env.DISPLAY_MODE === 'word' ? 'phrase' : 'word'; await sendDraftControls(ctx); }
        else if (op === 'tog_frame') { env.FRAME_MODE   = env.FRAME_MODE === 'original' ? 'vertical' : 'original'; await sendDraftControls(ctx); }
        else if (op === 'tog_reuse') { env.EXCLUDE_USED = env.EXCLUDE_USED === '1' ? '0' : '1'; await sendDraftControls(ctx); }
        else if (op === 'dur+')      { env.DURATION = String(Math.min(60, parseInt(env.DURATION) + 5)); await sendDraftControls(ctx); }
        else if (op === 'dur-')      { env.DURATION = String(Math.max(10, parseInt(env.DURATION) - 5)); await sendDraftControls(ctx); }
        else if (op === 'off+')      { env.OFFSET = String(parseInt(env.OFFSET) + 20); await sendDraftControls(ctx); }
        else if (op === 'nocap')     { env.CAPTIONS = env.CAPTIONS === 'none' ? 'auto' : 'none'; await sendDraftControls(ctx); }
        else if (op === 'render') {
            const track = tracksCache.find(t => t.id === env.TRACK_ID);
            if (!track) return ctx.reply('❌ Выбери трек!').catch(() => {});
            const audioPath = path.join(track.path, 'track.mp3');
            if (!fs.existsSync(audioPath)) return ctx.reply('❌ track.mp3 не найден!').catch(() => {});

            const finalEnv = { ...env }; drafts.delete(chatId);
            const editNum = await getNextEditNum();
            const seed = 1 + Math.floor(Math.random() * 9999);
            await ctx.reply(`🚀 Запуск #${editNum} (Трек: ${track.name})...`).catch(() => {});
            handleEditCallback(ctx, editNum, 'render', {
                customAudio: audioPath,
                customLyrics: path.join(track.path, 'text.txt'),
                env: finalEnv,
                seed
            });
        }
    } catch (err) {
        logger.error(`DraftCallback error op=${op}`, err);
        await ctx.answerCbQuery('❌ Ошибка').catch(() => {});
    }
    logger.info(`BUTTON_FINISHED callback=d:${op} time=${Date.now() - start}ms`);
}

async function handleEditCallback(ctx, editNum, op, options = {}) {
    const start = Date.now();
    if (op !== 'render') await ctx.answerCbQuery().catch(() => {});

    try {
        const env = options.env || defaultDraftEnv(null);
        const seed = options.seed || (1 + Math.floor(Math.random() * 9999));

        let audioPath = options.customAudio;
        if (!audioPath) {
            const state = await getActiveState();
            const track = tracksCache.find(t => t.id === state.track_id);
            if (track) audioPath = path.join(track.path, 'track.mp3');
        }
        if (!audioPath || !fs.existsSync(audioPath)) {
            return ctx.reply('❌ Аудио не найдено — выбери трек.').catch(() => {});
        }

        if (!fs.existsSync(REFS_DIR)) {
            return ctx.reply(`❌ Папка рефов не найдена: ${REFS_DIR}\nЗадай REFS_DIR в .env`).catch(() => {});
        }
        if (!fs.existsSync(OUT_DIR)) await fsp.mkdir(OUT_DIR, { recursive: true }).catch(() => {});
        const outPath = path.join(OUT_DIR, `edit_${editNum}_s${seed}.mp4`);

        const state = await getActiveState();
        const trackId = env.TRACK_ID || state.track_id;
        const track = tracksCache.find(t => t.id === trackId);

        const args = [
            path.join(SKILL_DIR, 'scripts', 'ai_edit.py'),
            '--sources', REFS_DIR,
            '--audio', audioPath,
            '--output', outPath,
            '--seed', String(seed)
        ];

        if (trackId) args.push('--track-id', trackId);

        if (options.customLyrics) {
            if (fs.existsSync(options.customLyrics)) {
                args.push('--lyrics', options.customLyrics);
            } else {
                return ctx.reply(`❌ Файл текста не найден: ${path.basename(options.customLyrics)}`).catch(() => {});
            }
        }

        if (track && Number.isInteger(track.meta.subtitle_start_line)) {
            args.push('--lyrics-start-line', String(track.meta.subtitle_start_line));
        }

        if (env.SHAKE === '1')                  args.push('--shake');
        if (env.DURATION)                       args.push('--duration', env.DURATION);
        if (env.OFFSET)                         args.push('--offset', env.OFFSET);
        if (env.LOOK)                           args.push('--look', env.LOOK);
        if (env.FRAME_MODE === 'original')      args.push('--original-frame');
        if (env.FONT_SIZE)                      args.push('--font-size', env.FONT_SIZE);
        if (env.FONT_FAMILY)                    args.push('--font-family', FONT_MAP[env.FONT_FAMILY] || env.FONT_FAMILY);
        if (env.DISPLAY_MODE)                   args.push('--display-mode', env.DISPLAY_MODE);
        if (env.EXPOSURE && env.EXPOSURE !== '0.0') args.push('--exposure', env.EXPOSURE);
        if (env.EXCLUDE_USED === '1')           args.push('--exclude-used');
        if (env.CAPTIONS === 'none')            args.push('--captions', 'none');
        if (env.CAPTIONS_ANIM)                  args.push('--caption-anim', env.CAPTIONS_ANIM);

        // Gracefully kill previous render of this chat
        const prev = activeProcesses.get(ctx.chat.id);
        if (prev) {
            try { killTree(prev.pid); } catch (_) {}
            await new Promise((res) => {
                const t = setTimeout(res, 3000);
                prev.once('close', () => { clearTimeout(t); res(); });
                prev.once('exit',  () => { clearTimeout(t); res(); });
            });
        }
        if (activeRenders >= MAX_CONCURRENT_RENDERS) {
            return ctx.reply('⏳ Занято. Попробуйте позже.').catch(() => {});
        }

        activeRenders++;
        let released = false;
        const release = () => {
            if (released) return;
            released = true;
            activeProcesses.delete(ctx.chat.id);
            activeRenders = Math.max(0, activeRenders - 1);
        };

        let errBuf = '';
        const child = spawn(PYTHON_EXE, args, {
            env: {
                ...process.env,
                PYTHONIOENCODING: 'utf-8',
                PYTHONUTF8: '1'
            },
            windowsHide: true
        });
        activeProcesses.set(ctx.chat.id, child);

        child.stderr.on('data', (d) => {
            if (errBuf.length < STDERR_CAP) errBuf += d.toString();
            console.log(`[Python Err] ${d}`);
        });
        child.stdout.on('data', (d) => { console.log(`[Python Out] ${d}`); });

        child.on('close', async (code) => {
            release();
            if (code === 0) {
                if (fs.existsSync(outPath)) {
                    await ctx.replyWithVideo({ source: fs.createReadStream(outPath) }, { caption: `✅ #${editNum} | seed=${seed}` }).catch(() => {});
                } else {
                    await ctx.reply(`❌ Файл не найден после рендера: ${path.basename(outPath)}`).catch(() => {});
                }
            } else {
                const tail = errBuf.trim().split('\n').slice(-5).join('\n');
                await ctx.reply(`❌ Ошибка rc=${code}\n\n${tail || 'Нет данных в stderr'}`).catch(() => {});
            }
        });
        child.on('error', async (err) => {
            logger.error('Render spawn error', err);
            release();
            await ctx.reply(`❌ Ошибка запуска: ${err.message}`).catch(() => {});
        });
    } catch (e) {
        logger.error('EditCallback fail', e);
        await ctx.reply('❌ Системная ошибка рендера').catch(() => {});
    }
    logger.info(`BUTTON_FINISHED callback=r:${editNum}:${op} time=${Date.now() - start}ms`);
}

async function sendStats(ctx) {
    let log = [];
    try { log = JSON.parse(await fsp.readFile(RENDER_LOG, 'utf8')); } catch (_) {}
    const total = Array.isArray(log) ? log.length : 0;
    const last  = total ? log[total - 1] : null;
    const lines = [
        `📊 **Статистика**`,
        `Активных рендеров: ${activeRenders}/${MAX_CONCURRENT_RENDERS}`,
        `Треков: ${tracksCache.length}`,
        `Всего эдитов: ${total}`,
        last ? `Последний: ${last.ts} (${last.track_id || last.audio || '?'})` : '',
    ].filter(Boolean);
    await ctx.reply(lines.join('\n'), { parse_mode: 'Markdown' }).catch(() => {});
}

async function sendRefsInfo(ctx) {
    let count = 0;
    try {
        const entries = await fsp.readdir(REFS_DIR);
        const exts = new Set(['.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v']);
        count = entries.filter(f => exts.has(path.extname(f).toLowerCase())).length;
    } catch (_) {}
    await ctx.reply(`📁 Рефов в ${REFS_DIR}: ${count}`).catch(() => {});
}

async function sendEdits(ctx) {
    let log = [];
    try { log = JSON.parse(await fsp.readFile(RENDER_LOG, 'utf8')); } catch (_) {}
    if (!Array.isArray(log) || !log.length) return ctx.reply('📋 Эдитов пока нет.').catch(() => {});
    const last = log.slice(-10).reverse();
    const lines = ['📋 **Последние эдиты:**', ...last.map(e =>
        `#${e.edit_num || '?'} • ${e.track_id || e.audio} • ${e.duration}s • ${e.size_mb}MB`
    )];
    await ctx.reply(lines.join('\n'), { parse_mode: 'Markdown' }).catch(() => {});
}

// --- BOT MIDDLEWARE & ROUTES ---
bot.use(async (ctx, next) => {
    if (!isAuthorized(ctx)) {
        if (ctx.callbackQuery) await ctx.answerCbQuery('⛔ Доступ запрещён').catch(() => {});
        return;
    }
    return next();
});

bot.action(/^d:(.+)$/, async (ctx) => { await handleDraftCallback(ctx, ctx.match[1]); });
bot.action(/^r:(\d+):(.+)$/, async (ctx) => { await handleEditCallback(ctx, parseInt(ctx.match[1]), ctx.match[2]); });

bot.action(/^admin:(.+)$/, async (ctx) => {
    await ctx.answerCbQuery().catch(() => {});
    try {
        const op = ctx.match[1];

        if (op === 'add') {
            setFSM(ctx.chat.id, { step: 'mp3' });
            await ctx.reply('Отправьте MP3 файл.').catch(() => {});
        }
        else if (op === 'list') {
            const t = await loadTracks();
            await ctx.editMessageText('🎵 Библиотека треков:', buildTrackListKeyboard(t)).catch(editMsgCatch);
        }
        else if (op.startsWith('view_')) {
            await sendTrackCard(ctx, op.slice(5));
        }
        else if (op.startsWith('edit_offset_')) {
            const trackId = op.slice(12);
            if (!tracksCache.find(t => t.id === trackId)) return;
            setFSM(ctx.chat.id, { step: 'edit_offset', trackId });
            await ctx.reply('Введите новое время начала текста (например, 00:18 или 18):').catch(() => {});
        }
        else if (op.startsWith('edit_subline_')) {
            const trackId = op.slice(13);
            const track = tracksCache.find(t => t.id === trackId);
            if (!track) return;
            let preview = '';
            try {
                const lyrics = await fsp.readFile(path.join(track.path, 'text.txt'), 'utf8');
                preview = lyrics.split('\n').slice(0, 10).map((l, i) => `${i + 1} ${l}`).join('\n');
            } catch (e) {}
            setFSM(ctx.chat.id, { step: 'edit_subline', trackId });
            await ctx.reply(`Текст трека:\n${preview}\n\nВведите номер строки:`).catch(() => {});
        }
        else if (op.startsWith('rename_')) {
            const trackId = op.slice(7);
            if (!tracksCache.find(t => t.id === trackId)) return;
            setFSM(ctx.chat.id, { step: 'rename', trackId });
            await ctx.reply('Введите новое название трека:').catch(() => {});
        }
        else if (op.startsWith('delete_')) {
            const trackId = op.slice(7);
            const track = tracksCache.find(t => t.id === trackId);
            if (!track) return;
            // Safety: убедиться что путь внутри TRACKS_DIR
            if (!isInsideDir(TRACKS_DIR, track.path)) {
                logger.error(`delete blocked: path outside TRACKS_DIR ${track.path}`);
                return ctx.reply('❌ Безопасность: удаление заблокировано.').catch(() => {});
            }
            await fsp.rm(track.path, { recursive: true, force: true }).catch(() => {});
            const t = await loadTracks();
            await ctx.reply('🗑 Удалено.').catch(() => {});
            await ctx.reply('🎵 Библиотека треков:', buildTrackListKeyboard(t)).catch(() => {});
        }
        else if (op === 'back_main') await sendDraftControls(ctx);
        else if (op === 'back')      await ctx.editMessageText('Управление:', buildAdminMenu()).catch(editMsgCatch);
    } catch (e) {
        logger.error('admin action fail', e);
        await ctx.reply('❌ Ошибка админ-действия.').catch(() => {});
    }
});

bot.on('text', async (ctx) => {
    const msg = ctx.message.text;
    const chatId = ctx.chat.id;
    const lowMsg = msg.toLowerCase();

    logger.info(`MESSAGE_RECEIVED chat=${chatId} text="${msg.slice(0, 80)}"`);

    // 0. Reference URL (IG/TikTok/YT/X) — highest priority, не блокируется FSM.
    //    FSM шага 'lyrics' исключаем — там пользователь может вставить ссылку
    //    как текст песни, не как реф.
    const fsmCheck = trackFSM.get(chatId);
    if (!fsmCheck || fsmCheck.step !== 'lyrics') {
        const urlMatch = msg.match(REF_URL_RE);
        if (urlMatch) {
            logger.info(`LINK_HANDLER_TRIGGERED ${urlMatch[0]}`);
            pushDownload(() => handleRefUrl(ctx, urlMatch[0]));
            return;
        }
    }

    // Main menu commands
    if (lowMsg.includes('эдит') || lowMsg.includes('сделать')) {
        trackFSM.delete(chatId);
        return await sendDraftControls(ctx);
    }
    if (lowMsg.includes('управление')) {
        trackFSM.delete(chatId);
        return await ctx.reply('🎵 Управление треками:', buildAdminMenu()).catch(() => {});
    }
    if (lowMsg.includes('стата') || lowMsg.includes('статистика')) {
        return await sendStats(ctx);
    }
    if (lowMsg.includes('рефы')) {
        return await sendRefsInfo(ctx);
    }
    if (lowMsg.includes('эдиты') || lowMsg.includes('история')) {
        return await sendEdits(ctx);
    }
    if (lowMsg.startsWith('/start')) {
        trackFSM.delete(chatId);
        await ctx.reply('👋 Меню:', Markup.keyboard([
            ['🎬 Сделать эдит', '🎵 Управление треками'],
            ['📊 Стата', '📁 Рефы'],
            ['📋 Эдиты']
        ]).resize().persistent());
        return await sendDraftControls(ctx);
    }

    // FSM handling
    const fsm = trackFSM.get(chatId);
    if (fsm) {
        if (lowMsg.includes('назад') || lowMsg.includes('отмена')) {
            trackFSM.delete(chatId);
            return await ctx.reply('❌ Действие отменено.').catch(() => {});
        }

        if (fsm.step === 'edit_offset') {
            let val = msg.trim();
            if (val.includes(':')) {
                const [m, s] = val.split(':').map(Number);
                val = m * 60 + s;
            } else {
                val = Number(val);
            }
            if (!isNaN(val) && val >= 0 && val < 3600) {
                await updateTrackMeta(fsm.trackId, { lyrics_start_time: val });
                trackFSM.delete(chatId);
                await ctx.reply('✅ Обновлено.').catch(() => {});
                await sendTrackCard(ctx, fsm.trackId);
            } else {
                await ctx.reply('❌ Неверный формат (00:00 или сек). Напишите «назад» для отмены:').catch(() => {});
            }
            return;
        }
        if (fsm.step === 'edit_subline') {
            const val = parseInt(msg.trim(), 10);
            const track = tracksCache.find(t => t.id === fsm.trackId);
            let maxLines = 9999;
            if (track) {
                try {
                    const txt = await fsp.readFile(path.join(track.path, 'text.txt'), 'utf8');
                    maxLines = txt.split('\n').length;
                } catch (_) {}
            }
            if (!isNaN(val) && val >= 1 && val <= maxLines) {
                await updateTrackMeta(fsm.trackId, { subtitle_start_line: val });
                trackFSM.delete(chatId);
                await ctx.reply('✅ Обновлено.').catch(() => {});
                await sendTrackCard(ctx, fsm.trackId);
            } else {
                await ctx.reply(`❌ Введите число 1..${maxLines} или «назад»:`).catch(() => {});
            }
            return;
        }
        if (fsm.step === 'rename') {
            const newName = msg.trim().slice(0, 80);
            if (!newName) return await ctx.reply('❌ Пустое имя. Попробуйте ещё раз или «назад»:').catch(() => {});
            await updateTrackMeta(fsm.trackId, { name: newName });
            trackFSM.delete(chatId);
            await ctx.reply('✅ Переименовано.').catch(() => {});
            await sendTrackCard(ctx, fsm.trackId);
            return;
        }
        if (fsm.step === 'lyrics') {
            const track = tracksCache.find(t => t.id === fsm.trackId);
            if (!track || !isInsideDir(TRACKS_DIR, track.path)) {
                trackFSM.delete(chatId);
                return await ctx.reply('❌ Трек не найден или путь небезопасен.').catch(() => {});
            }
            await fsp.writeFile(path.join(track.path, 'text.txt'), msg.trim(), 'utf8');
            trackFSM.delete(chatId);
            await ctx.reply('✅ Текст сохранён.').catch(() => {});
            await sendTrackCard(ctx, fsm.trackId);
            return;
        }
    }
});

bot.on(['audio', 'document'], async (ctx) => {
    const fsm = trackFSM.get(ctx.chat.id);
    if (!fsm) return;

    const doc = ctx.message.document || ctx.message.audio;
    if (!doc) return;
    const rawName = doc.file_name
        || (ctx.message.audio && (ctx.message.audio.title || ctx.message.audio.performer))
        || `track_${Date.now()}`;

    if (fsm.step === 'mp3') {
        if (!String(rawName).toLowerCase().endsWith('.mp3') && !(doc.mime_type || '').includes('audio')) {
            return ctx.reply('❌ Пожалуйста, отправьте MP3 файл.').catch(() => {});
        }
        const trackId = sanitizeFilename(rawName);
        if (!trackId) return ctx.reply('❌ Невалидное имя файла.').catch(() => {});

        const trackPath = path.join(TRACKS_DIR, trackId);
        if (!isInsideDir(TRACKS_DIR, trackPath)) {
            logger.error(`upload blocked: ${rawName} -> ${trackPath}`);
            return ctx.reply('❌ Безопасность: блокировка пути.').catch(() => {});
        }

        try {
            if (!fs.existsSync(trackPath)) await fsp.mkdir(trackPath, { recursive: true });
            const localPath = path.join(trackPath, 'track.mp3');
            await ctx.reply(`📥 Загружаю трек: ${rawName}...`).catch(() => {});
            const link = await ctx.telegram.getFileLink(doc.file_id);
            const resp = await axios({ url: link.href, responseType: 'stream', timeout: 120000 });
            const writer = fs.createWriteStream(localPath);
            resp.data.pipe(writer);
            await new Promise((res, rej) => {
                writer.on('finish', res);
                writer.on('error', rej);
                resp.data.on('error', rej);
            });

            const meta = {
                id: trackId,
                name: String(rawName).replace(/\.[^/.]+$/, ''),
                lyrics_start_time: 0,
                subtitle_start_line: 1
            };
            await fsp.writeFile(path.join(trackPath, 'metadata.json'), JSON.stringify(meta, null, 2));

            setFSM(ctx.chat.id, { step: 'lyrics', trackId, meta });
            await loadTracks();
            await ctx.reply(`✅ Трек «${meta.name}» загружен.\n\nТеперь отправьте текст песни (сообщением или .txt файлом):`).catch(() => {});
        } catch (e) {
            logger.error('Upload MP3 fail', e);
            await ctx.reply(`❌ Ошибка загрузки MP3: ${e.message}`).catch(() => {});
        }
    }
    else if (fsm.step === 'lyrics') {
        const isTxt = String(rawName).toLowerCase().endsWith('.txt');
        const track = tracksCache.find(t => t.id === fsm.trackId);
        if (!track || !isInsideDir(TRACKS_DIR, track.path)) {
            trackFSM.delete(ctx.chat.id);
            return ctx.reply('❌ Трек не найден или путь небезопасен.').catch(() => {});
        }

        try {
            if (isTxt) {
                await ctx.reply(`📥 Загружаю текст: ${rawName}...`).catch(() => {});
                const link = await ctx.telegram.getFileLink(doc.file_id);
                const resp = await axios({ url: link.href, responseType: 'arraybuffer', timeout: 60000 });
                await fsp.writeFile(path.join(track.path, 'text.txt'), Buffer.from(resp.data).toString('utf8'));
                trackFSM.delete(ctx.chat.id);
                await ctx.reply('✅ Текст загружен из файла.').catch(() => {});
                await sendTrackCard(ctx, fsm.trackId);
            } else {
                await ctx.reply('❌ Для текста нужен .txt файл или просто отправьте его сообщением.').catch(() => {});
            }
        } catch (e) {
            logger.error('Upload lyrics fail', e);
            await ctx.reply(`❌ Ошибка загрузки текста: ${e.message}`).catch(() => {});
        }
    }
});

loadTracks().then(() => bot.launch({ dropPendingUpdates: true }).then(() => console.log('Bot READY')));
process.once('SIGINT',  () => bot.stop('SIGINT'));
process.once('SIGTERM', () => bot.stop('SIGTERM'));
