/**
 * Stonks Run - scan scheduler (Cloudflare Worker + Cron Trigger)
 * ===============================================================
 *
 * WHY THIS EXISTS
 * GitHub's built-in `schedule:` trigger is best-effort. For this repo it
 * asked for 9 scans per weekday and GitHub actually ran 2-3, usually
 * starting 3-4 hours late - so the site sat on stale data every morning.
 * Cloudflare cron triggers fire on time, so this Worker calls GitHub's API
 * to start the "Scheduled scan" workflow exactly when it should run.
 *
 * WHAT IT DOES
 * The cron trigger fires at :17 and :47 past every hour (UTC). Each time,
 * the Worker converts the scheduled time to US Eastern and only starts a
 * scan if it is a weekday and one of SLOTS_ET below. Doing the time check
 * here (not in the cron expression) keeps it correct across daylight
 * saving changes with no edits.
 *
 * SETUP (full steps in READ-ME-FIRST.txt at the repo root)
 *   1. Secret GITHUB_TOKEN = a fine-grained GitHub token with access to
 *      ONLY the trend-ledger repo and "Actions: Read and write".
 *   2. Cron trigger: 17,47 * * * *
 *   3. Visit the Worker's URL: it shows the slots and checks the token.
 */

const OWNER = "fscottevens";
const REPO = "trend-ledger";
const WORKFLOW_FILE = "scan.yml";
const BRANCH = "main";

// Scan start times in US Eastern (24-hour), Monday-Friday.
// :47 so the first run lands after the 9:30 open plus Yahoo's ~15 min delay;
// 16:17 captures the closing prices. Every entry must be :17 or :47 to match
// the cron trigger. For every 30 minutes instead, add the :17 times
// ("10:17", "11:17", ...).
const SLOTS_ET = ["09:47", "10:47", "11:47", "12:47", "13:47", "14:47", "15:47", "16:17"];

function easternParts(date) {
  const fmt = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  });
  const p = {};
  for (const part of fmt.formatToParts(date)) p[part.type] = part.value;
  const hour = String(parseInt(p.hour, 10) % 24).padStart(2, "0");
  return { weekday: p.weekday, hhmm: `${hour}:${p.minute}` };
}

function isWeekday(parts) {
  return !["Sat", "Sun"].includes(parts.weekday);
}

function githubHeaders(env) {
  return {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "stonks-run-scheduler",
  };
}

async function dispatchScan(env, reason) {
  if (!env.GITHUB_TOKEN) throw new Error("GITHUB_TOKEN secret is not set on this Worker");
  const url = `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`;

  for (let attempt = 1; attempt <= 3; attempt++) {
    let status = 0;
    let detail = "";
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { ...githubHeaders(env), "Content-Type": "application/json" },
        body: JSON.stringify({ ref: BRANCH }),
      });
      status = res.status;
      if (res.ok) {
        console.log(`Scan dispatched (${reason}) - HTTP ${status}`);
        return;
      }
      detail = (await res.text()).slice(0, 300);
      // 4xx (bad/expired token, missing permission) will not fix itself.
      if (status >= 400 && status < 500) {
        throw new Error(`GitHub refused the dispatch (HTTP ${status}): ${detail}`);
      }
    } catch (err) {
      if (status >= 400 && status < 500) throw err;
      detail = String(err);
    }
    console.warn(`Dispatch attempt ${attempt} failed (HTTP ${status || "network"}): ${detail}`);
    if (attempt < 3) await new Promise((r) => setTimeout(r, attempt * 5000));
  }
  throw new Error(`Could not dispatch scan (${reason}) after 3 attempts`);
}

export default {
  // Runs on the cron trigger.
  async scheduled(controller, env, ctx) {
    const et = easternParts(new Date(controller.scheduledTime));
    if (!isWeekday(et) || !SLOTS_ET.includes(et.hhmm)) {
      console.log(`${et.weekday} ${et.hhmm} ET is not a scan slot - nothing to do.`);
      return;
    }
    ctx.waitUntil(dispatchScan(env, `${et.weekday} ${et.hhmm} ET`));
  },

  // Visiting the Worker URL shows a status page. It never starts a scan.
  async fetch(request, env) {
    if (new URL(request.url).pathname !== "/") return new Response("Not found", { status: 404 });

    const now = easternParts(new Date());
    let token;
    if (!env.GITHUB_TOKEN) {
      token = "MISSING - add a secret named GITHUB_TOKEN";
    } else {
      const res = await fetch(
        `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW_FILE}`,
        { headers: githubHeaders(env) },
      );
      token = res.ok
        ? "OK (can see the workflow)"
        : `ERROR HTTP ${res.status} - check the token's repo access and expiry`;
    }

    const body = [
      "Stonks Run scan scheduler",
      "",
      `Now (US Eastern):        ${now.weekday} ${now.hhmm}`,
      `Scan slots (ET, Mon-Fri): ${SLOTS_ET.join(", ")}`,
      `Workflow:                ${OWNER}/${REPO} .github/workflows/${WORKFLOW_FILE} @ ${BRANCH}`,
      `GitHub token:            ${token}`,
      "",
      "Scans started by this Worker show as 'workflow_dispatch' runs at",
      `https://github.com/${OWNER}/${REPO}/actions`,
    ].join("\n");
    return new Response(body, { headers: { "content-type": "text/plain; charset=utf-8" } });
  },
};
