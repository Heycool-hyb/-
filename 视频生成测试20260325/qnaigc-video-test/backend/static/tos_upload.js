const $ = (id) => document.getElementById(id);

const fileInput = $("fileInput");
const objectKeyPrefix = $("objectKeyPrefix");
const bucketEl = $("bucket");
const endpointEl = $("endpoint");
const domainEl = $("domain");
const uploadBtn = $("uploadBtn");
const resetBtn = $("resetBtn");

const progressBar = $("progressBar");
const statusEl = $("status");

function setStatus(text) {
  statusEl.textContent = text;
}

function setProgress(pct) {
  const v = Math.max(0, Math.min(100, Number(pct) || 0));
  progressBar.style.width = `${v}%`;
}

function resetUI() {
  setStatus("");
  setProgress(0);
  if (fileInput) fileInput.value = "";
}

async function presignAndUpload(file, objectKey) {
  const payload = {
    filename: file.name,
    content_type: file.type || "video/mp4",
    size: file.size,
    bucket: bucketEl.value.trim(),
    endpoint: endpointEl.value.trim(),
    object_key: objectKey,
    // optional: domain can help backend return object_url; not required for upload
    domain: domainEl.value.trim(),
  };

  setStatus("正在获取上传地址（presign）...");

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
    const msg = presignJson?.detail?.message || presignJson?.detail || presignJson?.message || presignBodyText || presignRes.statusText;
    throw new Error(msg);
  }

  // 预期返回：
  // - upload_url: 预签名 PUT URL
  // - headers: （可选）需要额外附带的 header
  // - object_url: （可选）最终可访问 URL
  const uploadUrl = presignJson.upload_url || presignJson.uploadUrl;
  const extraHeaders = presignJson.headers || {};
  const objectUrl = presignJson.object_url || presignJson.objectUrl;

  if (!uploadUrl) {
    throw new Error("presign 返回缺少 upload_url/uploadUrl。");
  }

  setStatus("上传中...");
  setProgress(0);

  await new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", uploadUrl, true);

    // 设置附带 header（若 presign 返回了）
    for (const [k, v] of Object.entries(extraHeaders || {})) {
      if (typeof v === "undefined" || v === null) continue;
      xhr.setRequestHeader(k, String(v));
    }

    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const pct = (e.loaded / e.total) * 100;
      setProgress(pct);
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve();
      } else {
        reject(new Error(`上传失败：HTTP ${xhr.status} ${xhr.responseText || ""}`));
      }
    };

    xhr.onerror = () => reject(new Error(`上传失败：网络错误`));

    // 直接把文件作为 body
    xhr.send(file);
  });

  if (objectUrl) {
    setStatus(`上传完成：${objectUrl}`);
  } else {
    setStatus("上传完成（未返回 object_url，请看控制台/后端返回内容）。");
  }
}

uploadBtn.addEventListener("click", async () => {
  try {
    if (!fileInput || !fileInput.files || fileInput.files.length === 0) {
      setStatus("请先选择一个视频文件。");
      return;
    }

    const file = fileInput.files[0];
    const prefix = objectKeyPrefix.value.trim() || "";
    const safePrefix = prefix.replace(/^\/+/, "");

    if (!bucketEl.value.trim()) {
      setStatus("请填写 Bucket（桶名）。");
      return;
    }
    if (!endpointEl.value.trim()) {
      setStatus("请填写 TOS Endpoint。");
      return;
    }

    const objectKey = `${safePrefix}${Date.now()}_${file.name}`;

    await presignAndUpload(file, objectKey);
  } catch (err) {
    setStatus(err.message || String(err));
  } finally {
    uploadBtn.disabled = false;
  }
});

resetBtn.addEventListener("click", () => {
  resetUI();
});

resetUI();

