const $ = (id) => document.getElementById(id);

const form = $("form");
const statusEl = $("status");
const videoWrap = $("videoWrap");
const videoEl = $("video");
const downloadLink = $("downloadLink");

const submitBtn = $("submitBtn");
const cancelBtn = $("cancelBtn");
const metricsTokensEl = $("metricsTokens");
const metricsDurationEl = $("metricsDuration");
const metricsCostEl = $("metricsCost");
const metricsHistoryEmptyEl = $("metricsHistoryEmpty");
const metricsHistoryListEl = $("metricsHistoryList");

let pollingTimer = null;
let jobStartedAtMs = null;
let pollTimeoutTimer = null;
let pollAttempt = 0;
let uiTickTimer = null;
let lastJobStatusSnapshot = null;
let lastStatusRenderKind = "idle"; // 'polling' | 'result' | 'idle'

const POLL_INTERVAL_MS = 3000;
const POLL_TIMEOUT_MS = 30 * 60 * 1000; // 最长轮询 30 分钟

function calcPollDelayMs(attempt) {
  // 指数退避：避免一直 3s 一次狂刷，同时不至于太慢到看不到进度
  const delay = Math.round(POLL_INTERVAL_MS * Math.pow(1.25, attempt));
  return Math.min(10000, delay); // 上限 10 秒
}

function setStatus(text) {
  statusEl.textContent = text;
}

function stopPolling() {
  if (pollingTimer) {
    clearTimeout(pollingTimer);
    pollingTimer = null;
  }
  if (pollTimeoutTimer) {
    clearTimeout(pollTimeoutTimer);
    pollTimeoutTimer = null;
  }
  if (uiTickTimer) {
    clearInterval(uiTickTimer);
    uiTickTimer = null;
  }
  cancelBtn.style.display = "none";
}

function formatSeconds(sec) {
  const s = Number(sec);
  if (!Number.isFinite(s) || s < 0) return "";
  if (s < 60) return `${Math.floor(s)}s`;
  const m = Math.floor(s / 60);
  const rs = s - m * 60;
  return `${m}m${Math.floor(rs)}s`;
}

function pickFirst(obj, paths) {
  for (const p of paths) {
    let cur = obj;
    let ok = true;
    for (const key of p) {
      if (!cur || typeof cur !== "object" || !(key in cur)) {
        ok = false;
        break;
      }
      cur = cur[key];
    }
    if (ok && (typeof cur === "number" || typeof cur === "string")) return cur;
  }
  return null;
}

function extractUsageSummary(s) {
  const tr = s?.task_result || s?.taskResult || {};
  const usage = tr?.usage || tr?.token_usage || tr?.tokenUsage || null;
  if (!usage || typeof usage !== "object") return null;

  const total =
    pickFirst(usage, [["total_tokens"], ["totalTokens"], ["tokens"], ["total"]]) ?? null;
  const input =
    pickFirst(usage, [["input_tokens"], ["inputTokens"], ["prompt_tokens"], ["promptTokens"], ["input"]]) ?? null;
  const output =
    pickFirst(usage, [["output_tokens"], ["outputTokens"], ["completion_tokens"], ["completionTokens"], ["output"]]) ?? null;

  const parts = [];
  if (total !== null) parts.push(`total=${total}`);
  if (input !== null) parts.push(`input=${input}`);
  if (output !== null) parts.push(`output=${output}`);
  if (parts.length) return parts.join(", ");

  try {
    return JSON.stringify(usage);
  } catch {
    return null;
  }
}

function extractUsageNumbers(s) {
  const tr = s?.task_result || s?.taskResult || {};
  const usage = tr?.usage || tr?.token_usage || tr?.tokenUsage || null;
  if (!usage || typeof usage !== "object") return null;

  const total =
    pickFirst(usage, [["total_tokens"], ["totalTokens"], ["tokens"], ["total"]]) ?? null;
  const input =
    pickFirst(usage, [["input_tokens"], ["inputTokens"], ["prompt_tokens"], ["promptTokens"], ["input"]]) ?? null;
  const output =
    pickFirst(usage, [["output_tokens"], ["outputTokens"], ["completion_tokens"], ["completionTokens"], ["output"]]) ?? null;

  const nt = total !== null ? Number(total) : null;
  const ni = input !== null ? Number(input) : null;
  const no = output !== null ? Number(output) : null;

  return {
    total: Number.isFinite(nt) ? nt : null,
    input: Number.isFinite(ni) ? ni : null,
    output: Number.isFinite(no) ? no : null,
  };
}

function extractVideoDurationSecondsFromTaskResult(taskResult) {
  const videos = taskResult?.videos;
  if (!Array.isArray(videos) || videos.length === 0) return null;
  const d = videos[0]?.duration;
  const n = Number(d);
  return Number.isFinite(n) && n > 0 ? n : null;
}

function setMetricsTokens(text) {
  if (!metricsTokensEl) return;
  metricsTokensEl.textContent = text || "—";
}

function setMetricsDuration(text) {
  if (!metricsDurationEl) return;
  metricsDurationEl.textContent = text || "—";
}

function setMetricsCost(text) {
  if (!metricsCostEl) return;
  metricsCostEl.textContent = text || "—";
}

function computeUnitPriceYuanPerSecond({ mode, hasReferenceVideo, hasSound }) {
  const m = (mode || "").toLowerCase();
  const ref = !!hasReferenceVideo;
  const sound = !!hasSound;

  // 按你截图的“元/秒”价格表逐条映射：
  // std x 无参考视频 x 无声 => 0.6
  // std x 有参考视频 x 无声 => 0.9
  // pro x 无参考视频 x 无声 => 0.8
  // pro x 有参考视频 x 无声 => 1.2
  // std x 无参考视频 x 有声 x 未指定音色 => 0.8
  // pro x 无参考视频 x 有声 x 未指定音色 => 1.0
  if (m === "std" && !ref && !sound) return 0.6;
  if (m === "std" && ref && !sound) return 0.9;
  if (m === "pro" && !ref && !sound) return 0.8;
  if (m === "pro" && ref && !sound) return 1.2;

  // “有声 x 未指定音色”在当前页面里我们无法从 UI 精确区分具体音色来源，
  // 暂时把它当作：sound=true 且 ref=false 时，对应截图的“未指定音色”价格。
  if (m === "std" && !ref && sound) return 0.8;
  if (m === "pro" && !ref && sound) return 1.0;

  // 你截图里没有给出：有参考视频 + 有声 的单价，所以不做推断
  return null;
}

function yuan(n) {
  const x = Number(n);
  if (!Number.isFinite(x)) return null;
  return Math.round(x * 100) / 100;
}

function updateMetrics({
  statusSnapshot,
  elapsedSeconds,
  expectedVideoSeconds,
  actualVideoSeconds,
  mode,
  hasReferenceVideo,
  hasSound,
}) {
  // Tokens
  const u = extractUsageNumbers(statusSnapshot);
  if (u && (u.total !== null || u.input !== null || u.output !== null)) {
    const parts = [];
    if (u.total !== null) parts.push(`total=${u.total}`);
    if (u.input !== null) parts.push(`input=${u.input}`);
    if (u.output !== null) parts.push(`output=${u.output}`);
    setMetricsTokens(parts.join(", ") || "—");
  } else {
    setMetricsTokens("—");
  }

  // Duration
  const elapsedTxt = Number.isFinite(elapsedSeconds) ? formatSeconds(elapsedSeconds) : "";
  const expectedTxt =
    Number.isFinite(expectedVideoSeconds) && expectedVideoSeconds
      ? formatSeconds(expectedVideoSeconds)
      : "";
  const actualTxt =
    Number.isFinite(actualVideoSeconds) && actualVideoSeconds ? formatSeconds(actualVideoSeconds) : "";

  const durationLines = [];
  if (elapsedTxt) durationLines.push(`生成耗时: ${elapsedTxt}`);
  if (actualTxt) durationLines.push(`视频时长: ${actualTxt}`);
  else if (expectedTxt) durationLines.push(`视频时长(预估): ${expectedTxt}`);
  setMetricsDuration(durationLines.join("\n") || "—");

  // Cost
  const unit = computeUnitPriceYuanPerSecond({ mode, hasReferenceVideo, hasSound });
  const secForCost =
    Number.isFinite(actualVideoSeconds) && actualVideoSeconds
      ? actualVideoSeconds
      : Number.isFinite(expectedVideoSeconds) && expectedVideoSeconds
        ? expectedVideoSeconds
        : null;

  if (!unit) {
    const m = (mode || "").toLowerCase();
    const refTxt = hasReferenceVideo ? "有参考视频" : "无参考视频";
    const soundTxt = hasSound ? "有声" : "无声";
    setMetricsCost(`未配置单价：${m} x ${refTxt} x ${soundTxt}（截图表内未给出该组合）`);
    return;
  }
  if (!secForCost) {
    setMetricsCost(`单价: ${unit} 元/秒\n费用: —`);
    return;
  }
  const total = yuan(unit * secForCost);
  setMetricsCost(`单价: ${unit} 元/秒\n费用: ${total}`);
}

function getTaskVideos(taskResult) {
  const videos = taskResult?.videos;
  return Array.isArray(videos) ? videos : [];
}

function appendMetricsHistoryRecord({
  jobId,
  statusSnapshot,
  expectedVideoSeconds,
  mode,
  hasReferenceVideo,
  hasSound,
}) {
  if (!metricsHistoryListEl) return;
  if (metricsHistoryEmptyEl) metricsHistoryEmptyEl.style.display = "none";

  const usage = extractUsageNumbers(statusSnapshot);
  const tokenText = usage
    ? [
        usage.total !== null ? `total=${usage.total}` : null,
        usage.input !== null ? `input=${usage.input}` : null,
        usage.output !== null ? `output=${usage.output}` : null,
      ]
        .filter(Boolean)
        .join(", ") || "—"
    : "—";

  const unit = computeUnitPriceYuanPerSecond({ mode, hasReferenceVideo, hasSound });
  const videos = getTaskVideos(statusSnapshot?.task_result || statusSnapshot?.taskResult);
  const rows = [];
  if (!videos.length) {
    rows.push({ idx: 1, duration: expectedVideoSeconds || null, cost: null, note: "未返回视频列表" });
  } else {
    for (let i = 0; i < videos.length; i += 1) {
      const v = videos[i];
      const d = Number(v?.duration);
      const sec = Number.isFinite(d) && d > 0 ? d : expectedVideoSeconds || null;
      const c = unit && sec ? yuan(unit * sec) : null;
      rows.push({ idx: i + 1, duration: sec, cost: c, url: v?.url || "" });
    }
  }

  const wrap = document.createElement("div");
  wrap.style.border = "1px solid #eee";
  wrap.style.borderRadius = "8px";
  wrap.style.padding = "10px";
  const createdAt = new Date().toLocaleString();

  const lines = rows
    .map((r) => {
      const durTxt = r.duration ? formatSeconds(r.duration) : "—";
      const costTxt =
        r.cost !== null
          ? `${r.cost} 元`
          : unit
            ? "—"
            : "未配置单价";
      const urlTxt = r.url ? `\n  URL: ${r.url}` : "";
      const noteTxt = r.note ? `\n  说明: ${r.note}` : "";
      return `视频 #${r.idx}\n  Token: ${tokenText}\n  时长: ${durTxt}\n  费用: ${costTxt}${urlTxt}${noteTxt}`;
    })
    .join("\n\n");

  wrap.textContent = `任务ID: ${jobId}\n时间: ${createdAt}\n模式: ${(mode || "").toLowerCase()}\n\n${lines}`;
  metricsHistoryListEl.prepend(wrap);
}

// ----- Persist form state -----
const STORAGE_KEY = "qnaigc-video-test:formState:v1";

function safeJsonParse(s) {
  try {
    return s ? JSON.parse(s) : null;
  } catch {
    return null;
  }
}

function loadFormState() {
  const raw = localStorage.getItem(STORAGE_KEY);
  return safeJsonParse(raw) || {};
}

function saveFormState(partial) {
  const prev = loadFormState();
  const next = { ...prev, ...partial, _ts: Date.now() };
  localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
}

function bindPersist(id, { event = "input", getValue, setValue } = {}) {
  const el = $(id);
  if (!el) return;
  const state = loadFormState();
  if (typeof state[id] !== "undefined" && state[id] !== null && state[id] !== "") {
    try {
      const v = state[id];
      if (setValue) setValue(el, v);
      else el.value = String(v);
    } catch {
      // ignore
    }
  }

  let t = null;
  const handler = () => {
    if (t) clearTimeout(t);
    t = setTimeout(() => {
      try {
        const v = getValue ? getValue(el) : el.value;
        saveFormState({ [id]: v });
      } catch {
        // ignore
      }
    }, 150);
  };
  el.addEventListener(event, handler);
}

function setupPersistence() {
  // 生成参数
  bindPersist("videoUrl", { event: "input" });
  bindPersist("prompt", { event: "input" });
  bindPersist("seconds", { event: "change" });
  bindPersist("size", { event: "change" });
  bindPersist("mode", { event: "change" });
  bindPersist("referType", { event: "change" });
  bindPersist("keepOriginalSound", { event: "change" });

  // TOS 参数
  bindPersist("tosObjectKeyPrefix", { event: "input" });
  bindPersist("tosBucket", { event: "input" });
  bindPersist("tosEndpoint", { event: "input" });
  bindPersist("tosDomain", { event: "input" });
}

async function createJob(payload) {
  let res = null;
  try {
    res = await fetch("/api/video-jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    throw new Error(`请求失败（无法连接到 /api/video-jobs）。${err.message || String(err)}`);
  }
  const bodyText = await res.text().catch(() => "");
  let parsed = null;
  try {
    parsed = bodyText ? JSON.parse(bodyText) : null;
  } catch {
    parsed = null;
  }

  if (!res.ok) {
    const detail = parsed?.detail ?? parsed;
    const responseField = detail?.response;
    const responseStr =
      responseField && typeof responseField === "object"
        ? JSON.stringify(responseField, null, 2)
        : responseField;
    const msg =
      responseStr ||
      detail?.message ||
      (typeof detail === "object" ? JSON.stringify(detail, null, 2) : detail) ||
      bodyText ||
      res.statusText;
    throw new Error(`create job failed: ${msg}`);
  }

  // 兼容后端返回纯 JSON / 或者为空
  return parsed ?? (bodyText ? JSON.parse(bodyText) : {});
}

async function getJobStatus(jobId) {
  let res = null;
  try {
    res = await fetch(`/api/video-jobs/${encodeURIComponent(jobId)}`);
  } catch (err) {
    throw new Error(
      `请求失败（无法连接到 /api/video-jobs/${encodeURIComponent(jobId)}）。${err.message || String(err)}`
    );
  }
  const bodyText = await res.text().catch(() => "");
  let parsed = null;
  try {
    parsed = bodyText ? JSON.parse(bodyText) : null;
  } catch {
    parsed = null;
  }

  if (!res.ok) {
    const detail = parsed?.detail ?? parsed;
    const responseField = detail?.response;
    const responseStr =
      responseField && typeof responseField === "object"
        ? JSON.stringify(responseField, null, 2)
        : responseField;
    const msg =
      responseStr ||
      detail?.message ||
      (typeof detail === "object" ? JSON.stringify(detail, null, 2) : detail) ||
      bodyText ||
      res.statusText;
    throw new Error(msg);
  }

  return parsed ?? (bodyText ? JSON.parse(bodyText) : {});
}

function buildPollingStatusText(s, elapsed) {
  const usageSummary = extractUsageSummary(s);
  return (
    `任务状态: ${s?.status}\n` +
    `任务ID: ${s?.id}` +
    (elapsed ? `\n已耗时: ${elapsed}` : "") +
    (usageSummary ? `\nToken: ${usageSummary}` : "") +
    (s?.error ? `\n错误: ${s.error.code || ""} ${s.error.message || ""}` : "")
  );
}

function firstVideoUrl(taskResult) {
  const videos = taskResult?.videos;
  if (!Array.isArray(videos) || videos.length === 0) return null;
  return videos[0]?.url || null;
}

async function downloadToLocal(videoUrl) {
  const res = await fetch("/api/save/generated-video", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ video_url: videoUrl }),
  });
  const bodyText = await res.text().catch(() => "");
  let parsed = null;
  try {
    parsed = bodyText ? JSON.parse(bodyText) : null;
  } catch {
    parsed = null;
  }

  if (!res.ok) {
    const msg = parsed?.detail?.message || parsed?.detail || parsed?.message || bodyText || res.statusText;
    throw new Error(msg);
  }
  return parsed?.video_url;
}

async function renderResult(taskResult) {
  lastStatusRenderKind = "result";
  const url = firstVideoUrl(taskResult);
  if (!url) {
    videoWrap.style.display = "none";
    setStatus((statusEl.textContent || "") + "\n没有拿到 task_result.videos[0].url");
    return;
  }

  videoWrap.style.display = "block";
  videoEl.src = url;
  downloadLink.href = url;

  setStatus("视频已生成，正在下载到本地...");
  try {
    const localUrl = await downloadToLocal(url);
    if (localUrl) {
      const localAbsUrl = toAbsoluteUrl(localUrl);
      videoEl.src = localAbsUrl;
      downloadLink.href = localAbsUrl;
      setStatus("视频已生成，并已下载到本地。");

      // 下载到本地后再上传到 TOS：避免跨域导致浏览器直接拉取 QNAIGC 原始视频失败
      try {
        setGeneratedTosStatus("已下载到本地，正在上传到 TOS...");
        await uploadLocalVideoToTos(localUrl);
      } catch (err) {
        setGeneratedTosStatus(`上传到 TOS 失败：${err.message || String(err)}`);
      }
    } else {
      setStatus("视频已生成，但本地下载接口未返回视频地址。");
      setGeneratedTosStatus("本地下载未返回视频地址，跳过自动上传到 TOS。");
    }
  } catch (err) {
    setStatus((statusEl.textContent || "") + `\n本地下载失败：${err.message || String(err)}`);
    setGeneratedTosStatus(`本地下载失败，无法上传到 TOS：${err.message || String(err)}`);
  }
}

function toAbsoluteUrl(url) {
  if (!url) return url;
  // 如果是相对路径（比如 /static/uploads/xxx.mp4），转成完整 http(s) 地址
  return new URL(url, window.location.origin).toString();
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  stopPolling();
  jobStartedAtMs = null;
  resetGeneratedTosUI();
  lastJobStatusSnapshot = null;
  lastStatusRenderKind = "polling";
  setMetricsTokens("—");
  setMetricsDuration("—");
  setMetricsCost("—");

  const manualUrl = $("videoUrl").value;

  if (!manualUrl) {
    setStatus("请先填写参考视频 URL（必须公网可访问）。");
    submitBtn.disabled = false;
    return;
  }

  const payload = {
    model: "kling-v3-omni",
    prompt: $("prompt").value,
    video_url: manualUrl,
    refer_type: $("referType").value,
    keep_original_sound: $("keepOriginalSound").value,
    seconds: $("seconds").value,
    size: $("size").value,
    mode: $("mode").value,
  };
  const hasReferenceVideo = !!payload.video_url;

  submitBtn.disabled = true;
  setStatus("正在创建视频任务...");
  videoWrap.style.display = "none";

  try {
    payload.video_url = toAbsoluteUrl(payload.video_url);

    const created = await createJob(payload);
    const jobId = created.id;
    jobStartedAtMs = Date.now();
    setStatus(`已创建任务: ${jobId}\n开始轮询状态...`);

    cancelBtn.style.display = "inline-block";

    pollAttempt = 0;
    uiTickTimer = setInterval(() => {
      if (lastStatusRenderKind !== "polling") return;
      if (!jobStartedAtMs) return;
      if (!lastJobStatusSnapshot) return;
      const elapsedSeconds = (Date.now() - jobStartedAtMs) / 1000;
      const elapsed = formatSeconds(elapsedSeconds);
      setStatus(buildPollingStatusText(lastJobStatusSnapshot, elapsed));

      updateMetrics({
        statusSnapshot: lastJobStatusSnapshot,
        elapsedSeconds,
        expectedVideoSeconds: Number(payload.seconds) || null,
        actualVideoSeconds: extractVideoDurationSecondsFromTaskResult(
          lastJobStatusSnapshot?.task_result || lastJobStatusSnapshot?.taskResult
        ),
        mode: payload.mode,
        hasReferenceVideo,
        hasSound: payload.keep_original_sound === "yes",
      });
    }, 1000);

    pollTimeoutTimer = setTimeout(() => {
      stopPolling();
      const elapsed = jobStartedAtMs ? formatSeconds((Date.now() - jobStartedAtMs) / 1000) : "";
      setStatus(
        (statusEl.textContent || "") +
          `\n已超过 ${Math.round((POLL_TIMEOUT_MS / 1000 / 60) * 10) / 10} 分钟，自动停止轮询（任务可能仍在生成）。` +
          (elapsed ? `\n已耗时: ${elapsed}` : "")
      );
    }, POLL_TIMEOUT_MS);

    async function pollOnce() {
      try {
        const s = await getJobStatus(jobId);
        lastJobStatusSnapshot = s;
        const elapsedSeconds = jobStartedAtMs ? (Date.now() - jobStartedAtMs) / 1000 : null;
        const elapsed = elapsedSeconds !== null ? formatSeconds(elapsedSeconds) : "";
        setStatus(buildPollingStatusText(s, elapsed));

        updateMetrics({
          statusSnapshot: s,
          elapsedSeconds: elapsedSeconds ?? null,
          expectedVideoSeconds: Number(payload.seconds) || null,
          actualVideoSeconds: extractVideoDurationSecondsFromTaskResult(s?.task_result || s?.taskResult),
          mode: payload.mode,
          hasReferenceVideo,
          hasSound: payload.keep_original_sound === "yes",
        });

        if (s.status === "completed") {
          stopPolling();
          await renderResult(s.task_result);

          // completed 后用实际视频时长再刷新一次费用（若接口给了 duration）
          const actualVideoSeconds = extractVideoDurationSecondsFromTaskResult(s?.task_result || s?.taskResult);
          const elapsedSecondsFinal = jobStartedAtMs ? (Date.now() - jobStartedAtMs) / 1000 : null;
          updateMetrics({
            statusSnapshot: s,
            elapsedSeconds: elapsedSecondsFinal ?? null,
            expectedVideoSeconds: Number(payload.seconds) || null,
            actualVideoSeconds,
            mode: payload.mode,
            hasReferenceVideo,
            hasSound: payload.keep_original_sound === "yes",
          });
          appendMetricsHistoryRecord({
            jobId,
            statusSnapshot: s,
            expectedVideoSeconds: Number(payload.seconds) || null,
            mode: payload.mode,
            hasReferenceVideo,
            hasSound: payload.keep_original_sound === "yes",
          });
          return;
        }
        if (["failed", "cancelled"].includes(s.status)) {
          stopPolling();
          await renderResult(s.task_result || {});
          return;
        }

        pollAttempt += 1;
        const nextDelayMs = calcPollDelayMs(pollAttempt);
        // 让 UI 有机会更新，避免过于紧密的 promise 链
        pollingTimer = setTimeout(pollOnce, nextDelayMs);
      } catch (err) {
        stopPolling();
        setStatus(`轮询出错: ${err.message || String(err)}`);
      }
    }

    // 先等一小段时间再开始轮询（避免创建任务瞬间就一连刷）
    pollingTimer = setTimeout(pollOnce, 300);
  } catch (err) {
    setStatus(err.message || String(err));
  } finally {
    submitBtn.disabled = false;
  }
});

cancelBtn.addEventListener("click", () => {
  stopPolling();
  setStatus(statusEl.textContent + "\n已停止轮询（任务后台仍可能在生成）。");
});

// ----- TOS upload (merged into homepage) -----
const tosFileInput = $("tosFileInput");
const tosObjectKeyPrefixEl = $("tosObjectKeyPrefix");
const tosBucketEl = $("tosBucket");
const tosEndpointEl = $("tosEndpoint");
const tosDomainEl = $("tosDomain");
const tosUploadBtn = $("tosUploadBtn");
const tosResetBtn = $("tosResetBtn");
const tosProgressBar = $("tosProgressBar");
const tosStatusEl = $("tosStatus");
const generatedTosStatusEl = $("generatedTosStatus");
const generatedTosLinkEl = $("generatedTosLink");

function setTosStatus(text) {
  if (!tosStatusEl) return;
  tosStatusEl.textContent = text;
}

function setTosProgress(pct) {
  if (!tosProgressBar) return;
  const v = Math.max(0, Math.min(100, Number(pct) || 0));
  tosProgressBar.style.width = `${v}%`;
}

function resetTosUI() {
  setTosStatus("");
  setTosProgress(0);
  if (tosFileInput) tosFileInput.value = "";
}

function setGeneratedTosStatus(text) {
  if (!generatedTosStatusEl) return;
  generatedTosStatusEl.textContent = text;
}

function setGeneratedTosUrl(url) {
  if (!generatedTosLinkEl) return;
  if (!url) {
    generatedTosLinkEl.href = "#";
    generatedTosLinkEl.style.display = "none";
    return;
  }
  generatedTosLinkEl.href = url;
  generatedTosLinkEl.textContent = url;
  generatedTosLinkEl.style.display = "inline-block";
}

function resetGeneratedTosUI() {
  setGeneratedTosStatus("尚未生成。");
  setGeneratedTosUrl(null);
}

async function uploadLocalVideoToTos(localUrl) {
  const bucket = tosBucketEl?.value?.trim();
  const endpoint = tosEndpointEl?.value?.trim();
  if (!bucket || !endpoint) {
    setGeneratedTosStatus("未填写 Bucket/Endpoint，跳过自动上传到 TOS。");
    return null;
  }
  if (!localUrl) throw new Error("localUrl is empty");

  setGeneratedTosStatus("正在上传生成结果到 TOS...");

  const absLocalUrl = toAbsoluteUrl(localUrl);
  const resp = await fetch(absLocalUrl);
  if (!resp.ok) throw new Error(`获取本地视频失败：HTTP ${resp.status}`);

  const headerType = (resp.headers.get("content-type") || "").toLowerCase();
  // 防止隧道兜底页/错误页面被当成视频上传到 TOS
  if (headerType && !headerType.startsWith("video/")) {
    const txt = await resp.text().catch(() => "");
    throw new Error(
      `获取本地视频失败：预期视频但返回 content-type=${headerType}。` +
        `可能是隧道未工作/路径不通。返回预览：${(txt || "").slice(0, 200)}`
    );
  }

  const blob = await resp.blob();
  const contentType = resp.headers.get("content-type") || blob.type || "video/mp4";

  let baseName = "generated.mp4";
  try {
    baseName = new URL(absLocalUrl).pathname.split("/").pop() || baseName;
  } catch {
    // ignore
  }

  const file = new File([blob], baseName, { type: contentType });

  const prefix = tosObjectKeyPrefixEl?.value?.trim() || "";
  const safePrefix = prefix.replace(/^\/+/, "");
  const objectKey = `${safePrefix}${Date.now()}_${baseName}`;

  const objectUrl = await presignAndUploadToTos(file, objectKey, {
    backfillVideoUrl: false,
  });

  setGeneratedTosStatus("上传成功。");
  setGeneratedTosUrl(objectUrl);
  return objectUrl;
}

async function presignAndUploadToTos(file, objectKey, options = {}) {
  const { backfillVideoUrl = true } = options;
  const payload = {
    filename: file.name,
    content_type: file.type || "video/mp4",
    size: file.size,
    bucket: tosBucketEl?.value?.trim(),
    endpoint: tosEndpointEl?.value?.trim(),
    object_key: objectKey,
    domain: tosDomainEl?.value?.trim(),
  };

  if (!payload.bucket) throw new Error("请先填写 Bucket（桶名）。");
  if (!payload.endpoint) throw new Error("请先填写 TOS Endpoint。");

  setTosProgress(0);
  setTosStatus("正在获取上传地址（presign）...");

  const presignRes = await fetch("/api/tos/presign", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  const presignBodyText = await presignRes.text().catch(() => "");
  let presignJson = null;
  try {
    presignJson = presignBodyText ? JSON.parse(presignBodyText) : null;
  } catch {
    presignJson = null;
  }

  if (!presignRes.ok) {
    const msg =
      presignJson?.detail?.message ||
      presignJson?.detail ||
      presignJson?.message ||
      presignBodyText ||
      presignRes.statusText;
    throw new Error(msg);
  }

  const uploadUrl = presignJson?.upload_url || presignJson?.uploadUrl;
  const extraHeaders = presignJson?.headers || {};
  const objectUrl = presignJson?.object_url || presignJson?.objectUrl;

  if (!uploadUrl) throw new Error("presign 返回缺少 upload_url/uploadUrl。");

  setTosStatus("上传中...");
  setTosProgress(0);

  await new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", uploadUrl, true);

    for (const [k, v] of Object.entries(extraHeaders || {})) {
      if (typeof v === "undefined" || v === null) continue;
      xhr.setRequestHeader(k, String(v));
    }

    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const pct = (e.loaded / e.total) * 100;
      setTosProgress(pct);
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else reject(new Error(`上传失败：HTTP ${xhr.status} ${xhr.responseText || ""}`));
    };
    xhr.onerror = () => reject(new Error("上传失败：网络错误"));
    xhr.send(file);
  });

  if (!objectUrl) {
    setTosStatus("上传完成（未返回 object_url）。");
    throw new Error("上传完成但后端未返回 object_url（请检查 domain/endpoint/bucket 配置）。");
  }

  setTosStatus(`上传完成：${objectUrl}`);
  if (backfillVideoUrl) {
    const videoUrlInput = $("videoUrl");
    if (videoUrlInput) videoUrlInput.value = objectUrl;
    setStatus("已上传并回填参考视频 URL。请下方填写 prompt 并提交生成。");
  }
  return objectUrl;
}

if (tosUploadBtn) {
  tosUploadBtn.addEventListener("click", async () => {
    try {
      if (!tosFileInput || !tosFileInput.files || tosFileInput.files.length === 0) {
        setTosStatus("请先选择一个视频文件。");
        return;
      }
      const file = tosFileInput.files[0];

      const prefix = tosObjectKeyPrefixEl?.value?.trim() || "";
      const safePrefix = prefix.replace(/^\/+/, "");
      const objectKey = `${safePrefix}${Date.now()}_${file.name}`;

      tosUploadBtn.disabled = true;
      await presignAndUploadToTos(file, objectKey);
    } catch (err) {
      setTosStatus(err.message || String(err));
    } finally {
      tosUploadBtn.disabled = false;
    }
  });
}

if (tosResetBtn) {
  tosResetBtn.addEventListener("click", () => resetTosUI());
}

resetTosUI();
resetGeneratedTosUI();

setupPersistence();

