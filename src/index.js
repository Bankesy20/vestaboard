// Same Note layout as weather_board.py. Cloudflare runs this every hour.
// 24-hour rain is millimetres. 7-day rain is centimetres. F is Beaufort force.

const COLS = 15;
const EA_ROOT = "https://environment.data.gov.uk/flood-monitoring";
const NRW_ROOT = "https://rivers-and-seas.naturalresources.wales";
const LOCATIONS = [
  { name: "COPS", lat: 51.75235, lon: -2.14116, source: "ea", station: "1141" },
  { name: "FAGW", lat: 52.01267, lon: -4.89712, source: "nrw", station: "Maenclochog" },
];

const CHAR_CODES = {
  " ": 0, A: 1, B: 2, C: 3, D: 4, E: 5, F: 6, G: 7, H: 8, I: 9, J: 10, K: 11,
  L: 12, M: 13, N: 14, O: 15, P: 16, Q: 17, R: 18, S: 19, T: 20, U: 21, V: 22,
  W: 23, X: 24, Y: 25, Z: 26, 1: 27, 2: 28, 3: 29, 4: 30, 5: 31, 6: 32, 7: 33,
  8: 34, 9: 35, 0: 36, "!": 37, "@": 38, "#": 39, $: 40, "(": 41, ")": 42,
  "-": 44, "+": 46, "&": 47, "=": 48, ";": 49, ":": 50, "'": 52, '"': 53,
  "%": 54, ",": 55, ".": 56, "/": 59, "?": 60, "°": 62,
};

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(sendBoard(env));
  },
};

async function sendBoard(env) {
  if (!env.VESTABOARD_TOKEN) throw new Error("VESTABOARD_TOKEN is not set");
  const lines = await boardLines();
  const characters = lines.map((line) =>
    [...line].map((char) => CHAR_CODES[char] ?? CHAR_CODES[char.toUpperCase()] ?? 0),
  );
  const response = await fetch("https://cloud.vestaboard.com/", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Vestaboard-Token": env.VESTABOARD_TOKEN,
    },
    body: JSON.stringify({ characters }),
  });
  if (!response.ok) {
    throw new Error(`Vestaboard rejected the message (${response.status})`);
  }
}

async function boardLines() {
  const now = Date.now();
  const rows = [];
  for (const location of LOCATIONS) {
    const [temp, wind] = await currentConditions(location.lat, location.lon);
    const rain =
      location.source === "nrw"
        ? await nrwRainfall(location.station, now)
        : await eaRainfall(location.station, now);
    rows.push(locationLine(location.name, temp, rain.mm24, rain.mm7, beaufort(wind)));
  }
  return [headerLine(), ...rows];
}

async function getJson(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`${response.status} from ${url}`);
  return response.json();
}

function asList(value) {
  if (value == null) return [];
  return Array.isArray(value) ? value : [value];
}

async function eaRainfall(station, now) {
  const since = new Date(now - 7 * 86400000).toISOString().replace(/\.\d{3}Z$/, "Z");
  const readings = new Map();
  let offset = 0;
  for (;;) {
    const url =
      `${EA_ROOT}/id/stations/${encodeURIComponent(station)}/readings` +
      `?parameter=rainfall&since=${encodeURIComponent(since)}&_limit=2000&_offset=${offset}`;
    const items = asList((await getJson(url)).items);
    for (const item of items) {
      if (item.value == null || !item.dateTime) continue;
      readings.set(item.dateTime, Number(item.value));
    }
    if (items.length < 2000) break;
    offset += 2000;
  }
  return sumReadings(readings, now);
}

async function nrwRainfall(stationName, now) {
  const stations = asList(await getJson(`${NRW_ROOT}/map/GetStations`));
  const match = stations.find(
    (station) => String(station.name?.english ?? "").toLowerCase() === stationName.toLowerCase(),
  );
  if (!match) throw new Error(`No Natural Resources Wales station named ${stationName}`);
  const parameter = (match.parameters ?? []).find(
    (item) => item.typeId === 2 || item.typeText?.english === "Rainfall",
  );
  if (!parameter) throw new Error(`${stationName} has no rainfall parameter`);
  const start = new Date(now - 7 * 86400000).toISOString().replace(/\.\d{3}Z$/, "Z");
  const end = new Date(now).toISOString().replace(/\.\d{3}Z$/, "Z");
  const url =
    `${NRW_ROOT}/graph/getdata?parameterId=${parameter.id}` +
    `&from=${encodeURIComponent(start)}&to=${encodeURIComponent(end)}`;
  const readings = new Map();
  for (const point of (await getJson(url)).data ?? []) {
    if (point.y == null || !point.x) continue;
    readings.set(point.x, Number(point.y));
  }
  return sumReadings(readings, now);
}

function sumReadings(readings, now) {
  const cutoff = now - 86400000;
  let mm24 = 0;
  let mm7 = 0;
  for (const [stamp, value] of readings) {
    const when = Date.parse(stamp);
    mm7 += value;
    if (when >= cutoff) mm24 += value;
  }
  return { mm24, mm7 };
}

async function currentConditions(lat, lon) {
  const url =
    "https://api.open-meteo.com/v1/forecast" +
    `?latitude=${lat}&longitude=${lon}&current=temperature_2m,wind_speed_10m&wind_speed_unit=ms`;
  const current = (await getJson(url)).current ?? {};
  return [current.temperature_2m ?? null, current.wind_speed_10m ?? null];
}

function beaufort(windMs) {
  if (windMs == null) return null;
  const limits = [0.3, 1.6, 3.4, 5.5, 8.0, 10.8, 13.9, 17.2, 20.8, 24.5, 28.5, 32.7];
  const force = limits.findIndex((limit) => windMs < limit);
  return force === -1 ? 12 : force;
}

function roundHalfEven(value) {
  const floor = Math.floor(value);
  const fraction = value - floor;
  if (Math.abs(fraction - 0.5) < 1e-8) return floor % 2 === 0 ? floor : floor + 1;
  return Math.round(value);
}

function formatMm(value, width) {
  if (value == null) return "?".padStart(width);
  let text;
  if (width === 2 && value < 0.95) text = `.${Math.min(9, roundHalfEven(value * 10))}`;
  else text = String(roundHalfEven(value));
  if (text.length > width) text = String(roundHalfEven(value));
  return text.slice(-width).padStart(width);
}

function formatTemp(value) {
  if (value == null) return "??";
  return String(roundHalfEven(value)).padStart(2).slice(-2);
}

function locationLine(name, temp, mm24, mm7, force) {
  const chars = Array(COLS).fill(" ");
  chars.splice(0, 4, ...name.slice(0, 4).padEnd(4));
  chars.splice(5, 2, ...formatTemp(temp));
  chars.splice(8, 2, ...formatMm(mm24, 2));
  chars.splice(11, 2, ...formatMm(mm7 == null ? null : mm7 / 10, 2));
  const forceText = force == null ? "?" : String(Math.max(0, Math.min(12, force)));
  if (forceText.length === 1) chars[14] = forceText;
  else chars.splice(13, 2, ...forceText.slice(-2));
  const line = chars.join("");
  if (line.length !== COLS) throw new Error(`line is ${line.length} chars: ${line}`);
  return line;
}

function headerLine() {
  const chars = Array(COLS).fill(" ");
  chars.splice(5, 2, ..."°C");
  chars.splice(8, 2, ..."24");
  chars.splice(11, 2, ..."7D");
  chars[14] = "F";
  return chars.join("");
}
