const form = document.querySelector("#download-form");
const homeButton = document.querySelector("#home-button");
const input = document.querySelector("#url-input");
const scanButton = document.querySelector("#scan-button");
const statusBox = document.querySelector("#game-status");
const progressShell = document.querySelector("#progress-shell");
const progressBar = document.querySelector("#progress-bar");
const resultPanel = document.querySelector("#result-panel");
const footerNote = document.querySelector("#footer-note");
const videoThumb = document.querySelector("#video-thumb");
const videoTitle = document.querySelector("#video-title");
const videoUploader = document.querySelector("#video-uploader");
const videoOptions = document.querySelector("#video-options");
const audioOptions = document.querySelector("#audio-options");

let currentUrl = "";
let progressTimer = null;

function setStatus(message, type = "") {
    statusBox.textContent = message;
    statusBox.className = `game-status ${type}`.trim();
}

function setLoading(isLoading, text = "DOWNLOAD") {
    scanButton.disabled = isLoading;
    scanButton.textContent = text;
    input.disabled = isLoading;
}

function setProgress(percent) {
    if (!Number.isFinite(percent)) {
        progressShell.hidden = true;
        progressBar.style.width = "0%";
        return;
    }

    progressShell.hidden = false;
    progressBar.style.width = `${Math.max(0, Math.min(100, percent))}%`;
}

function clearOptions() {
    videoOptions.innerHTML = "";
    audioOptions.innerHTML = "";
    resultPanel.hidden = true;
    videoThumb.removeAttribute("src");
    videoThumb.alt = "";
    setProgress(null);
}

function resetHome() {
    stopProgressTimer();
    currentUrl = "";
    form.reset();
    input.disabled = false;
    scanButton.disabled = false;
    scanButton.textContent = "DOWNLOAD";
    setStatus("");
    clearOptions();
    footerNote.textContent = "INSERT COIN TO PLAY";
    input.focus();
}

function formatDuration(seconds) {
    if (!Number.isFinite(seconds)) {
        return "";
    }

    const total = Math.max(0, Math.floor(seconds));
    const minutes = Math.floor(total / 60);
    const remainingSeconds = String(total % 60).padStart(2, "0");
    return `${minutes}:${remainingSeconds}`;
}

function createOptionButton(option, mode) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "option-btn";
    const mainLabel = option.shortLabel || option.label;
    const subLabel = option.label.startsWith(mainLabel)
        ? option.label.slice(mainLabel.length).replace(/^\s*-?\s*/, "")
        : "";
    button.innerHTML = `
        <span class="option-main">${mainLabel}</span>
        <span class="option-sub">${subLabel}</span>
    `;
    button.addEventListener("click", () => downloadOption(option, mode, button));
    return button;
}

function renderResult(data) {
    const duration = formatDuration(data.duration);
    videoTitle.textContent = data.title;
    videoUploader.textContent = duration
        ? `${data.uploader} // ${duration}`
        : data.uploader;

    if (data.thumbnail) {
        videoThumb.src = data.thumbnail;
        videoThumb.alt = data.title;
        videoThumb.hidden = false;
    } else {
        videoThumb.hidden = true;
    }

    videoOptions.innerHTML = "";
    audioOptions.innerHTML = "";

    data.videoOptions.forEach((option) => {
        videoOptions.append(createOptionButton(option, "video"));
    });

    data.audioOptions.forEach((option) => {
        audioOptions.append(createOptionButton(option, "audio"));
    });

    resultPanel.hidden = false;
}

async function inspectLink(url) {
    const response = await fetch("/api/inspect", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
        },
        body: JSON.stringify({ url }),
    });

    const data = await response.json().catch(() => ({}));

    if (!response.ok) {
        throw new Error(getErrorMessage(data));
    }

    return data;
}

function getErrorMessage(errorData, fallback = "TRY ANOTHER LINK") {
    if (!errorData) {
        return fallback;
    }

    if (typeof errorData.detail === "string") {
        return errorData.detail;
    }

    if (errorData.detail?.message) {
        return errorData.detail.message;
    }

    if (errorData.error) {
        return errorData.error;
    }

    return fallback;
}

async function startDownload(option, mode) {
    const response = await fetch("/api/download/start", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
        },
        body: JSON.stringify({
            url: currentUrl,
            mode,
            quality: option.quality || null,
            extension: option.extension,
        }),
    });

    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(getErrorMessage(data));
    }

    return data.jobId;
}

async function pollDownload(jobId) {
    const response = await fetch(`/api/download/progress/${jobId}`);
    const data = await response.json().catch(() => ({}));

    if (!response.ok) {
        throw new Error(getErrorMessage(data));
    }

    return data;
}

function stopProgressTimer() {
    if (progressTimer) {
        clearInterval(progressTimer);
        progressTimer = null;
    }
}

function waitForReady(jobId) {
    return new Promise((resolve, reject) => {
        stopProgressTimer();
        progressTimer = setInterval(async () => {
            try {
                const progress = await pollDownload(jobId);
                const percent = Number.isFinite(progress.progress) ? progress.progress : 0;

                if (progress.status === "error") {
                    stopProgressTimer();
                    reject(new Error(progress.error || "TRY ANOTHER LINK"));
                    return;
                }

                setProgress(percent);
                setStatus(
                    Number.isFinite(progress.progress)
                        ? `${progress.stage} ${percent}%`
                        : progress.stage,
                    "loading"
                );
                footerNote.textContent = progress.filename || "LOADING PRIZE...";

                if (progress.status === "ready") {
                    stopProgressTimer();
                    resolve(progress);
                }
            } catch (error) {
                stopProgressTimer();
                reject(error);
            }
        }, 700);
    });
}

async function fetchFile(jobId, extension) {
    const response = await fetch(`/api/download/file/${jobId}`);

    if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(getErrorMessage(error));
    }

    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename\*=UTF-8''([^;]+)/);
    const filename = match ? decodeURIComponent(match[1]) : `retrotube.${extension}`;
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
}

async function downloadOption(option, mode, button) {
    const originalHtml = button.innerHTML;
    button.disabled = true;
    button.innerHTML = `
        <span class="option-main">WAIT...</span>
        <span class="option-sub">0%</span>
    `;
    setProgress(0);
    setStatus("PREPARING 0%", "loading");
    footerNote.textContent = "LOADING PRIZE...";

    try {
        const jobId = await startDownload(option, mode);
        await waitForReady(jobId);
        button.innerHTML = `
            <span class="option-main">READY</span>
            <span class="option-sub">100%</span>
        `;
        await fetchFile(jobId, option.extension);
        setStatus("YOU WIN", "win");
        footerNote.textContent = "READY PLAYER ONE";
        setProgress(100);
    } catch (error) {
        stopProgressTimer();
        setStatus("GAME OVER", "lose");
        footerNote.textContent = error.message || "TRY ANOTHER LINK";
        setProgress(null);
    } finally {
        button.disabled = false;
        setTimeout(() => {
            button.innerHTML = originalHtml;
        }, 600);
    }
}

form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const url = input.value.trim();

    if (!url) {
        setStatus("GAME OVER", "lose");
        footerNote.textContent = "INSERT A VALID LINK";
        return;
    }

    currentUrl = url;
    clearOptions();
    setProgress(15);
    setStatus("SCANNING 15%", "loading");
    footerNote.textContent = "SCANNING SIGNAL...";
    setLoading(true, "WAIT...");

    try {
        const data = await inspectLink(url);
        renderResult(data);
        setStatus(data.status || "YOU WIN", "win");
        footerNote.textContent = "CHOOSE YOUR PRIZE";
        setProgress(100);
    } catch (error) {
        setStatus("GAME OVER", "lose");
        footerNote.textContent = error.message || "TRY ANOTHER LINK";
        setProgress(null);
    } finally {
        setLoading(false);
    }
});

homeButton.addEventListener("click", resetHome);
