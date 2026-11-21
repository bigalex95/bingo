'use strict';

const LETTERS = ['B', 'I', 'N', 'G', 'O'];
const FREE = 12;
const SESSION_KEY = 'bingo.session'; // sessionStorage: переподключение этой вкладки после перезагрузки
const LAST_KEY = 'bingo.last';       // localStorage: кнопка «Вернуться в комнату»
const NAME_KEY = 'bingo.name';
const SOUND_KEY = 'bingo.sound';
const CYR = { А: 'A', В: 'B', С: 'C', Е: 'E', Н: 'H', К: 'K', М: 'M', О: 'O', Р: 'P', Т: 'T', Х: 'X', У: 'Y' };

const $ = (sel) => document.querySelector(sel);
const letterOf = (n) => LETTERS[Math.floor((n - 1) / 15)];
const normCode = (s) => String(s || '').toUpperCase().replace(/[А-ЯЁ]/g, (ch) => CYR[ch] || '').replace(/[^A-Z]/g, '').slice(0, 4);
const readJson = (storage, key) => { try { return JSON.parse(storage.getItem(key)); } catch { return null; } };

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

let session = null;      // { room, token, name }
let state = null;        // последнее состояние с сервера
let es = null;           // EventSource
let firstState = true;
let lastDrawnCount = 0;
let lastLogId = 0;
let winnersKey = '';
let playersKey = '';
let renderedLogId = -1;
let nextDrawAt = null;
let soundOn = localStorage.getItem(SOUND_KEY) !== 'off';
const cardCells = [];
const boardCells = {};

/* =================== Сеть =================== */

async function api(path, body) {
  let res;
  try {
    res = await fetch(path, body
      ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
      : { cache: 'no-store' });
  } catch {
    const err = new Error('Нет связи с сервером');
    err.status = 0;
    throw err;
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || `Ошибка ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

async function act(action, extra = {}) {
  if (!session) return null;
  try {
    return await api('/api/action', { room: session.room, token: session.token, action, ...extra });
  } catch (err) {
    toast(err.message, 'error');
    return null;
  }
}

const checkUrl = (s) => `/api/check?room=${encodeURIComponent(s.room)}&token=${encodeURIComponent(s.token)}`;

function connect() {
  if (es) es.close();
  const source = new EventSource(`/api/events?room=${encodeURIComponent(session.room)}&token=${encodeURIComponent(session.token)}`);
  es = source;
  source.onopen = () => setConn(true);
  source.onmessage = (e) => {
    if (es !== source) return;
    const msg = JSON.parse(e.data);
    if (msg.kicked) return exitRoom('Ведущий убрал вас из комнаты');
    if (msg.left) return;
    setConn(true);
    onState(msg);
  };
  source.onerror = () => {
    if (es !== source) return;
    setConn(false);
    if (source.readyState === EventSource.CLOSED) setTimeout(() => recheck(source), 1500);
  };
}

async function recheck(source) {
  if (es !== source || !session) return;
  try {
    await api(checkUrl(session));
    connect();
  } catch (err) {
    if (err.status === 404) exitRoom('Комната больше не существует');
    else setTimeout(() => recheck(source), 3000);
  }
}

function setConn(ok) {
  $('#conn').textContent = ok ? '' : '⚠ Переподключение…';
}

/* =================== Вход и выход =================== */

function enterRoom(data) {
  session = { room: data.room, token: data.token, name: data.name || '' };
  sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
  localStorage.setItem(LAST_KEY, JSON.stringify(session));
  history.replaceState(null, '', `?room=${session.room}`);
  state = null;
  firstState = true;
  lastDrawnCount = 0;
  lastLogId = 0;
  winnersKey = '';
  playersKey = '';
  renderedLogId = -1;
  $('#home').classList.add('hidden');
  $('#game').classList.remove('hidden');
  $('#roomCode').textContent = session.room;
  connect();
}

function exitRoom(message) {
  if (es) es.close();
  es = null;
  session = null;
  state = null;
  sessionStorage.removeItem(SESSION_KEY);
  localStorage.removeItem(LAST_KEY);
  history.replaceState(null, '', location.pathname);
  $('#winnerOverlay').classList.add('hidden');
  $('#game').classList.add('hidden');
  document.body.classList.remove('is-host');
  showHome('');
  if (message) toast(message, 'warn');
}

function showHome(invited) {
  $('#home').classList.remove('hidden');
  $('#nameInput').value = localStorage.getItem(NAME_KEY) || '';
  $('#codeInput').value = invited || '';
  const note = $('#inviteNote');
  note.classList.toggle('hidden', !invited);
  note.textContent = invited ? `Вас пригласили в комнату ${invited}. Введите имя и нажмите «Войти».` : '';

  const resume = $('#resumeBtn');
  resume.classList.add('hidden');
  const last = readJson(localStorage, LAST_KEY);
  if (last && last.room && (!invited || invited === last.room)) {
    api(checkUrl(last)).then((info) => {
      resume.textContent = `↩ Вернуться в комнату ${info.room} как ${info.name}`;
      resume.onclick = () => enterRoom({ ...last, name: info.name });
      resume.classList.remove('hidden');
    }).catch(() => localStorage.removeItem(LAST_KEY));
  }
  (localStorage.getItem(NAME_KEY) ? (invited ? $('#joinBtn') : $('#createBtn')) : $('#nameInput')).focus();
}

function getName() {
  const name = $('#nameInput').value.trim();
  if (!name) {
    toast('Сначала введите имя', 'warn');
    $('#nameInput').focus();
    shake($('#nameInput'));
    return null;
  }
  localStorage.setItem(NAME_KEY, name);
  return name;
}

async function createRoom() {
  const name = getName();
  if (!name) return;
  unlockAudio();
  try {
    enterRoom({ ...(await api('/api/create', { name })), name });
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function joinRoom() {
  const code = normCode($('#codeInput').value);
  if (code.length !== 4) {
    toast('Код комнаты — 4 латинские буквы', 'warn');
    $('#codeInput').focus();
    return;
  }
  const name = getName();
  if (!name) return;
  unlockAudio();
  try {
    enterRoom({ ...(await api('/api/join', { room: code, name })), name });
  } catch (err) {
    toast(err.message, 'error');
  }
}

/* =================== Состояние =================== */

function onState(s) {
  const newRound = state && s.round !== state.round;
  const newBall = !firstState && !newRound && s.drawn.length > lastDrawnCount;
  lastDrawnCount = s.drawn.length;

  const freshLogs = s.log.filter((l) => l.id > lastLogId);
  if (s.log.length) lastLogId = Math.max(lastLogId, s.log[s.log.length - 1].id);

  nextDrawAt = s.nextDrawIn == null ? null : performance.now() + s.nextDrawIn * 1000;
  state = s;
  render(newBall);

  if (newBall) announce(s.drawn[s.drawn.length - 1]);
  if (!firstState) {
    for (const l of freshLogs) {
      if (l.kind === 'false') toast(l.text, 'warn');
      else if (l.kind === 'join' || l.kind === 'start') toast(l.text, 'info');
    }
  }

  const key = s.winners.length ? `${s.round}:${s.winners.map((w) => w.id).join(',')}` : '';
  if (key && key !== winnersKey) showWinners(!firstState);
  if (!key) $('#winnerOverlay').classList.add('hidden');
  winnersKey = key;
  firstState = false;
}

const isHost = () => state && state.hostId === state.you.id;
const myWin = () => state.winners.find((w) => w.id === state.you.id);

/* =================== Отрисовка =================== */

function render(newBall) {
  const s = state;
  document.body.classList.toggle('is-host', isHost());
  $('#roomCode').textContent = s.room;
  $('#roundBadge').textContent = `Раунд ${s.round}`;
  $('#patternBadge').textContent = `🎯 ${s.patterns[s.pattern]}`;
  document.title = s.drawn.length ? `${letterOf(s.drawn.at(-1))}-${s.drawn.at(-1)} · Бинго ${s.room}` : `Бинго ${s.room}`;
  renderBalls(newBall);
  renderStatus();
  renderHostControls();
  renderCard();
  renderPlayers();
  renderLog();
  renderBoard();
}

function setBall(node, n) {
  node.dataset.letter = n ? letterOf(n) : '';
  node.querySelector('.letter').textContent = n ? letterOf(n) : '';
  node.querySelector('.num').textContent = n || '?';
  node.classList.toggle('empty', !n);
}

function makeBall(n) {
  const ball = el('div', 'ball small');
  const inner = el('div', 'inner');
  inner.append(el('span', 'letter'), el('span', 'num'));
  ball.append(inner);
  setBall(ball, n);
  return ball;
}

function renderBalls(animate) {
  const drawn = state.drawn;
  const ball = $('#currentBall');
  setBall(ball, drawn.at(-1));
  if (animate) {
    ball.classList.remove('pop');
    void ball.offsetWidth;
    ball.classList.add('pop');
  }
  $('#recentBalls').replaceChildren(...drawn.slice(-6, -1).reverse().map(makeBall));
  $('#drawnCount').textContent = `${drawn.length} из 75`;
}

function renderStatus() {
  const s = state;
  const host = s.players.find((p) => p.id === s.hostId);
  let text;
  if (s.status === 'lobby') {
    text = isHost()
      ? 'Вы ведущий. Выберите цель и начните раунд, когда все соберутся.'
      : `Ждём, пока ${host ? host.name : 'ведущий'} начнёт раунд…`;
  } else if (s.status === 'playing') {
    text = s.drawn.length
      ? `Цель: ${s.patterns[s.pattern].toLowerCase()}. Отмечайте выпавшие числа и жмите «БИНГО!»`
      : 'Раунд начался! Сейчас выпадет первый шар…';
  } else {
    text = `Раунд окончен. ${s.winners.length > 1 ? 'Победители' : 'Победитель'}: ${s.winners.map((w) => w.name).join(', ')}`;
  }
  $('#statusText').textContent = text;
}

// Введённое значение интервала; если поле пустое или мусор — оставляем текущий интервал
function readInterval() {
  const v = parseFloat(String($('#intervalInput').value).replace(',', '.'));
  return Number.isFinite(v) ? v : state.interval;
}

// Сервер сам приводит значение к 1–60 сек. с шагом 0.5
function setAutoInterval(sec) {
  act('auto', { on: state.auto, interval: sec });
}

function renderHostControls() {
  const s = state;
  $('#hostControls').dataset.status = s.status;

  const chips = $('#patternChips');
  if (!chips.children.length) {
    for (const [key, label] of Object.entries(s.patterns)) {
      const chip = el('button', 'chip', label);
      chip.dataset.pattern = key;
      chip.onclick = () => act('pattern', { value: key });
      chips.append(chip);
    }
  }
  for (const chip of chips.children) chip.classList.toggle('active', chip.dataset.pattern === s.pattern);
  $('#allowAutoMarkChk').checked = s.autoMarkAllowed;

  const intervalInput = $('#intervalInput');
  if (document.activeElement !== intervalInput) intervalInput.value = String(s.interval);
  const intervalChips = $('#intervalChips');
  if (!intervalChips.children.length) {
    for (const sec of s.intervals) {
      const chip = el('button', 'chip', `${sec} с`);
      chip.dataset.sec = sec;
      chip.onclick = () => setAutoInterval(sec);
      intervalChips.append(chip);
    }
  }
  for (const chip of intervalChips.children) chip.classList.toggle('active', Number(chip.dataset.sec) === s.interval);

  const autoBtn = $('#autoBtn');
  autoBtn.textContent = s.auto ? '⏸ Авто: вкл' : '⏵ Авто: выкл';
  autoBtn.classList.toggle('on', s.auto);
  $('#drawBtn').disabled = s.drawn.length >= 75;
}

function buildCard() {
  const card = $('#card');
  for (const letter of LETTERS) card.append(el('div', 'head', letter));
  for (let i = 0; i < 25; i++) {
    const cell = el('button', 'cell');
    cell.type = 'button';
    cell.append(el('span'));
    cell.onclick = () => onCellClick(i);
    card.append(cell);
    cardCells.push(cell);
  }
}

function renderCard() {
  const { card, marks } = state.you;
  const markSet = new Set(marks);
  const win = myWin();
  const winCells = new Set(win ? win.cells : []);
  cardCells.forEach((cell, i) => {
    const n = card[i];
    cell.firstChild.textContent = i === FREE ? '★' : n;
    cell.title = i === FREE ? 'Бесплатная клетка' : `${letterOf(n)}-${n}`;
    cell.classList.toggle('free', i === FREE);
    cell.classList.toggle('marked', markSet.has(i));
    cell.classList.toggle('win', winCells.has(i));
  });

  const bingo = $('#bingoBtn');
  // после окончания раунда сервер ещё принимает ничьи на том же шаре (одновременные нажатия),
  // но кнопку гасим, чтобы она не провоцировала всех жать её зря
  bingo.disabled = state.status !== 'playing';

  const autoMarkAllowed = state.autoMarkAllowed;
  $('#autoMarkChk').checked = autoMarkAllowed && state.you.autoMark;
  $('#autoMarkChk').disabled = !autoMarkAllowed;
  $('#autoMarkSwitch').classList.toggle('disabled', !autoMarkAllowed);
  $('#autoMarkOff').classList.toggle('hidden', autoMarkAllowed);
  $('#newCardBtn').classList.toggle('hidden', state.status !== 'lobby');
}

function renderPlayers() {
  const s = state;
  const key = JSON.stringify([s.players, s.hostId, s.you.id]);
  if (key === playersKey) return;
  playersKey = key;

  const host = isHost();
  const list = $('#players');
  list.replaceChildren();
  const sorted = [...s.players].sort((a, b) => b.wins - a.wins);
  for (const p of sorted) {
    const li = el('li', [p.id === s.you.id && 'me', !p.online && 'off'].filter(Boolean).join(' '));
    li.append(el('span', `dot${p.online ? ' on' : ''}`));
    const label = `${p.id === s.hostId ? '👑 ' : ''}${p.name}${p.id === s.you.id ? ' (вы)' : ''}`;
    const name = el('span', 'pname', label);
    name.title = p.online ? label : `${label} — не в сети`;
    li.append(name);
    if (p.wins) li.append(el('span', 'wins', `🏆 ${p.wins}`));
    if (host && p.id !== s.you.id) {
      const actions = el('span', 'pact');
      const crown = el('button', '', '👑');
      crown.title = `Передать роль ведущего: ${p.name}`;
      crown.onclick = () => confirm(`Передать роль ведущего игроку ${p.name}?`) && act('make_host', { target: p.id });
      const kick = el('button', '', '✕');
      kick.title = `Убрать из комнаты: ${p.name}`;
      kick.onclick = () => confirm(`Убрать ${p.name} из комнаты?`) && act('kick', { target: p.id });
      actions.append(crown, kick);
      li.append(actions);
    }
    list.append(li);
  }
  $('#playerCount').textContent = `${s.players.filter((p) => p.online).length} в сети`;
}

function renderLog() {
  const last = state.log.length ? state.log.at(-1).id : 0;
  if (last === renderedLogId) return;
  renderedLogId = last;
  const list = $('#log');
  list.replaceChildren(...[...state.log].reverse().map((l) => {
    const li = el('li', l.kind);
    const time = el('time', '', new Date(l.t).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }));
    li.append(time, document.createTextNode(l.text));
    return li;
  }));
}

function buildBoard() {
  const board = $('#board');
  LETTERS.forEach((letter, row) => {
    board.append(el('div', `row-letter L-${letter}`, letter));
    for (let k = 1; k <= 15; k++) {
      const n = row * 15 + k;
      const cell = el('div', `bn L-${letter}`, n);
      boardCells[n] = cell;
      board.append(cell);
    }
  });
}

function renderBoard() {
  const drawn = new Set(state.drawn);
  const last = state.drawn.at(-1);
  for (let n = 1; n <= 75; n++) {
    boardCells[n].classList.toggle('on', drawn.has(n));
    boardCells[n].classList.toggle('last', n === last);
  }
}

/* =================== Действия игрока =================== */

function onCellClick(i) {
  if (!state || i === FREE) return;
  if (state.status === 'lobby') return toast('Раунд ещё не начался', 'info');
  const n = state.you.card[i];
  const marks = new Set(state.you.marks);
  if (!marks.has(i) && !state.drawn.includes(n)) {
    shake(cardCells[i]);
    return toast(`${letterOf(n)}-${n} ещё не выпадало`, 'warn');
  }
  const wasMarked = marks.has(i);
  wasMarked ? marks.delete(i) : marks.add(i);
  state.you.marks = [...marks];
  renderCard();
  act('mark', { cell: i }).then((res) => {
    if (res || !state) return;
    const back = new Set(state.you.marks);
    wasMarked ? back.add(i) : back.delete(i);
    state.you.marks = [...back];
    renderCard();
  });
}

async function claimBingo() {
  if ($('#bingoBtn').disabled) return;
  const res = await act('claim');
  if (res && !res.valid) {
    shake($('#card'));
    toast('Пока не бинго — проверьте карточку 😉', 'warn');
  }
}

function showWinners(celebrate) {
  const s = state;
  const mine = myWin();
  const shown = mine || s.winners[0];
  $('#winnerNames').textContent = `${s.winners.length > 1 ? 'Победители' : 'Побеждает'}: ${s.winners.map((w) => w.name).join(', ')}`;
  $('#winnerYou').classList.toggle('hidden', !mine);
  const drawn = new Set(s.drawn);
  const wins = new Set(shown.cells);
  $('#winnerCard').replaceChildren(...shown.card.map((n, i) => {
    const cls = wins.has(i) ? 'win' : (i === FREE || drawn.has(n)) ? 'drawn' : '';
    return el('div', cls, i === FREE ? '★' : n);
  }));
  $('#winnerOverlay').classList.remove('hidden');
  if (celebrate) {
    confetti();
    fanfare();
  }
}

/* =================== Звук =================== */

let audioCtx = null;
function unlockAudio() {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === 'suspended') audioCtx.resume();
  } catch { /* звук недоступен */ }
  return audioCtx;
}

function tone(freq, at, dur, type = 'sine', vol = 0.12) {
  const ctx = audioCtx;
  if (!ctx || ctx.state !== 'running') return;
  const t0 = ctx.currentTime + at;
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = type;
  osc.frequency.value = freq;
  gain.gain.setValueAtTime(0.0001, t0);
  gain.gain.exponentialRampToValueAtTime(vol, t0 + 0.02);
  gain.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
  osc.connect(gain).connect(ctx.destination);
  osc.start(t0);
  osc.stop(t0 + dur + 0.05);
}

function speak(text) {
  if (!('speechSynthesis' in window)) return;
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.lang = 'ru-RU';
  const voice = speechSynthesis.getVoices().find((v) => v.lang && v.lang.toLowerCase().startsWith('ru'));
  if (voice) u.voice = voice;
  u.rate = 0.95;
  speechSynthesis.speak(u);
}

function announce(n) {
  if (!soundOn) return;
  tone(660, 0, 0.18, 'triangle');
  tone(990, 0.09, 0.3, 'triangle');
  setTimeout(() => speak(`${letterOf(n)}, ${n}`), 350);
}

function fanfare() {
  if (!soundOn) return;
  [523, 659, 784, 1047, 784, 1047].forEach((f, i) => tone(f, i * 0.12, 0.35, 'triangle', 0.14));
}

function renderSoundBtn() {
  const btn = $('#soundBtn');
  btn.textContent = soundOn ? '🔊' : '🔇';
  btn.title = soundOn ? 'Звук и озвучка включены' : 'Звук выключен';
}

/* =================== Мелочи интерфейса =================== */

function toast(text, kind = 'info') {
  const node = el('div', `toast ${kind}`, text);
  const box = $('#toasts');
  box.append(node);
  while (box.children.length > 4) box.firstChild.remove();
  setTimeout(() => node.classList.add('hide'), 3200);
  setTimeout(() => node.remove(), 3700);
}

function shake(node) {
  node.classList.remove('shake');
  void node.offsetWidth;
  node.classList.add('shake');
}

async function copyInvite() {
  const link = `${location.origin}/?room=${session.room}`;
  try {
    await navigator.clipboard.writeText(link);
    toast('Ссылка-приглашение скопирована', 'ok');
  } catch {
    const ta = el('textarea');
    ta.value = link;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.append(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { /* ignore */ }
    ta.remove();
    if (ok) toast('Ссылка-приглашение скопирована', 'ok');
    else prompt('Скопируйте ссылку-приглашение:', link);
  }
}

function confetti() {
  const canvas = $('#confetti');
  const ctx = canvas.getContext('2d');
  const W = (canvas.width = innerWidth);
  const H = (canvas.height = innerHeight);
  const colors = ['#3b82f6', '#ef4444', '#a855f7', '#22c55e', '#f59e0b', '#ffffff', '#ff3d7f'];
  const parts = Array.from({ length: 220 }, () => ({
    x: W / 2 + (Math.random() - 0.5) * W * 0.3,
    y: H * 0.45,
    vx: (Math.random() - 0.5) * 18,
    vy: -Math.random() * 16 - 6,
    w: 6 + Math.random() * 6,
    h: 4 + Math.random() * 4,
    r: Math.random() * Math.PI,
    vr: (Math.random() - 0.5) * 0.35,
    c: colors[Math.floor(Math.random() * colors.length)],
  }));
  const start = performance.now();
  const frame = (now) => {
    const t = now - start;
    ctx.clearRect(0, 0, W, H);
    ctx.globalAlpha = Math.max(0, 1 - Math.max(0, t - 2800) / 1200);
    for (const p of parts) {
      p.vy += 0.38;
      p.vx *= 0.985;
      p.x += p.vx;
      p.y += p.vy;
      p.r += p.vr;
      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(p.r);
      ctx.fillStyle = p.c;
      ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h);
      ctx.restore();
    }
    if (t < 4000) requestAnimationFrame(frame);
    else ctx.clearRect(0, 0, W, H);
  };
  requestAnimationFrame(frame);
}

function tickCountdown() {
  const box = $('#countdown');
  if (state && state.auto && state.status === 'playing' && nextDrawAt) {
    const left = Math.max(0, nextDrawAt - performance.now());
    box.classList.add('on');
    $('#countdownBar').style.width = `${Math.min(100, (left / (state.interval * 1000)) * 100)}%`;
  } else {
    box.classList.remove('on');
  }
  requestAnimationFrame(tickCountdown);
}

/* =================== Привязка событий =================== */

function bindUi() {
  buildCard();
  buildBoard();
  renderSoundBtn();

  $('#createBtn').onclick = createRoom;
  $('#joinBtn').onclick = joinRoom;
  $('#nameInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') (normCode($('#codeInput').value).length === 4 ? joinRoom : createRoom)();
  });
  $('#codeInput').addEventListener('input', (e) => { e.target.value = normCode(e.target.value); });
  $('#codeInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') joinRoom(); });

  $('#copyLinkBtn').onclick = copyInvite;
  $('#soundBtn').onclick = () => {
    soundOn = !soundOn;
    localStorage.setItem(SOUND_KEY, soundOn ? 'on' : 'off');
    renderSoundBtn();
    if (soundOn) { unlockAudio(); tone(880, 0, 0.15); } else if ('speechSynthesis' in window) speechSynthesis.cancel();
  };
  $('#leaveBtn').onclick = async () => {
    if (!confirm('Выйти из комнаты? Ваши победы в этой комнате пропадут.')) return;
    await act('leave');
    exitRoom();
  };

  $('#startBtn').onclick = () => act('start');
  $('#drawBtn').onclick = () => act('draw');
  $('#autoBtn').onclick = () => act('auto', { on: !state.auto, interval: readInterval() });
  $('#intervalInput').onchange = () => setAutoInterval(readInterval());
  $('#intervalInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') e.target.blur(); });
  $('#stopRoundBtn').onclick = () => confirm('Прервать раунд? У всех будут новые карточки.') && act('new_round');
  $('#newRoundBtn').onclick = () => act('new_round');
  $('#overlayNewRound').onclick = () => act('new_round');
  $('#overlayClose').onclick = () => $('#winnerOverlay').classList.add('hidden');

  $('#bingoBtn').onclick = claimBingo;
  $('#autoMarkChk').onchange = (e) => act('auto_mark', { on: e.target.checked });
  $('#allowAutoMarkChk').onchange = (e) => act('allow_auto_mark', { on: e.target.checked });
  $('#newCardBtn').onclick = () => act('new_card');

  document.addEventListener('pointerdown', unlockAudio, { once: true });
  document.addEventListener('keydown', (e) => {
    if (!state || e.target.matches('input, select, textarea') || e.repeat) return;
    if (e.code === 'Space' && isHost() && state.status === 'playing') {
      e.preventDefault();
      if (e.target.matches('button')) e.target.blur(); // иначе кнопка в фокусе тоже «нажмётся»
      act('draw');
    }
    if (e.key === 'Escape') $('#winnerOverlay').classList.add('hidden');
  });
  if ('speechSynthesis' in window) speechSynthesis.getVoices();
  requestAnimationFrame(tickCountdown);
}

async function boot() {
  bindUi();
  const invited = normCode(new URLSearchParams(location.search).get('room'));
  const saved = readJson(sessionStorage, SESSION_KEY);
  if (saved && saved.room && (!invited || invited === saved.room)) {
    try {
      const info = await api(checkUrl(saved));
      enterRoom({ ...saved, name: info.name });
      return;
    } catch {
      sessionStorage.removeItem(SESSION_KEY);
    }
  }
  showHome(invited);
}

boot();
