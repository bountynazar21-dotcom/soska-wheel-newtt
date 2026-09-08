const tg = window.Telegram?.WebApp;

if (tg) {
  tg.ready();
  tg.expand();
}

const btn = document.getElementById("spinBtn");
const res = document.getElementById("result");
const pointerRotator = document.getElementById("pointer-rotator");
const fireworks = document.getElementById("fireworks");
const fireworksText = document.getElementById("fireworks-text");

const appStatus = document.getElementById("appStatus");
const wheelSection = document.getElementById("wheelSection");
const closedState = document.getElementById("closedState");

let spinning = false;
let currentRotation = 0;
let transitionFallbackTimer = null;
let wheelClosed = false;

// =========================
// СЕКТОРИ КОЛЕСА
// =========================
//
// ПОРЯДОК ВІД ВЕРХУ ЗА ГОДИННИКОВОЮ:
//
// 0 — Косметичка OXVA
// 1 — Панамка
// 2 — Сумка
// 3 — XLIM 3 Ultra
// 4 — Окуляри OXVA
// 5 — Кепка
//
const sectors = [
  "Косметичка OXVA",
  "Панамка",
  "Сумка",
  "XLIM 3 Ultra",
  "Окуляри OXVA",
  "Кепка"
];

const SECTOR_ANGLE = 360 / sectors.length;

// Сектор 0 стоїть зверху по центру.
const SAFE_CENTER_OFFSET = 0;

// Мікрокалібрування стрілки.
const POINTER_OFFSET = 0;

// Технічний fallback.
const FALLBACK_SECTOR_INDEX = 0;

// Технічні результати backend, які НЕ є реальними виграшами.
const TECHNICAL_RESULTS = new Set([
  "Помилка",
  "Нічого",
  "Подарунки закінчились"
]);


// =========================
// UI HELPERS
// =========================

function setResult(text, type = "default") {
  if (!res) return;

  res.textContent = text || "";

  res.classList.remove(
    "result-default",
    "result-win",
    "result-empty",
    "result-error",
    "result-repeat",
    "result-prank"
  );

  res.classList.add(`result-${type}`);
}


function setAppStatus(text = "") {
  if (!appStatus) return;

  appStatus.textContent = text;
}


function setButtonLoading(isLoading) {
  if (!btn) return;

  btn.disabled = isLoading || wheelClosed;
  btn.textContent = isLoading ? "Крутимо..." : "🎡 Крутити";
}


function stopFireworks() {
  if (!fireworks) return;

  fireworks.classList.remove("show");
}


function showClosedState(message = "") {
  wheelClosed = true;
  spinning = false;

  if (transitionFallbackTimer) {
    clearTimeout(transitionFallbackTimer);
    transitionFallbackTimer = null;
  }

  stopFireworks();

  if (pointerRotator) {
    pointerRotator.style.transition = "none";
  }

  if (wheelSection) {
    wheelSection.hidden = true;
  }

  if (closedState) {
    closedState.hidden = false;
  }

  if (btn) {
    btn.disabled = true;
  }

  if (message) {
    setAppStatus(message);
  } else {
    setAppStatus("");
  }

  try {
    tg?.HapticFeedback?.notificationOccurred("warning");
  } catch (_) {
    // Haptic feedback не критичний.
  }
}


function showWheelState() {
  if (wheelClosed) return;

  if (wheelSection) {
    wheelSection.hidden = false;
  }

  if (closedState) {
    closedState.hidden = true;
  }
}


// =========================
// TELEGRAM / API
// =========================

function getTelegramUserData() {
  let username = "unknown";
  let user_id = null;

  if (tg?.initDataUnsafe?.user) {
    const u = tg.initDataUnsafe.user;

    username =
      u.username ||
      `${u.first_name || ""} ${u.last_name || ""}`.trim() ||
      "user";

    user_id = u.id;
  }

  return {
    username,
    user_id,
    initData: tg?.initData || ""
  };
}


async function spinRequest(payload) {
  try {
    const response = await fetch("/spin", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify(payload)
    });

    let data;

    try {
      data = await response.json();
    } catch (_) {
      data = null;
    }

    if (!data || typeof data !== "object") {
      throw new Error("Backend returned invalid JSON");
    }

    return {
      ...data,
      http_ok: response.ok,
      http_status: response.status,
      client_error: false
    };
  } catch (error) {
    console.error("Spin request failed:", error);

    return {
      prize: "Помилка",
      sector_index: FALLBACK_SECTOR_INDEX,
      repeat: true,
      message: "Не вдалося зв’язатися із сервером. Спробуй ще раз.",
      http_ok: false,
      http_status: 0,
      client_error: true
    };
  }
}


// =========================
// RESULT CLASSIFICATION
// =========================

function isPrankText(text) {
  return (
    typeof text === "string" &&
    (
      text.toLowerCase().includes("попався") ||
      text.toLowerCase().includes("шпіоніро")
    )
  );
}


function isFundDepleted(data) {
  if (!data) return false;

  if (
    data.repeat === true &&
    data.prize === "Подарунки закінчились"
  ) {
    return true;
  }

  const message = String(data.message || "").toLowerCase();

  return (
    data.repeat === true &&
    (
      message.includes("подарунки") &&
      (
        message.includes("закінчилися") ||
        message.includes("закінчились")
      )
    )
  );
}


function isRealWin(prize) {
  if (!prize) return false;
  if (TECHNICAL_RESULTS.has(prize)) return false;
  if (isPrankText(prize)) return false;

  return sectors.includes(prize);
}


function isTechnicalNoPrize(prize) {
  return (
    prize === "Нічого" ||
    prize === "Подарунки закінчились"
  );
}


// =========================
// FIREWORKS
// =========================

function showFireworks(text) {
  if (!fireworks || !fireworksText) return;
  if (!isRealWin(text)) return;

  fireworksText.textContent = `🎉 ${text} 🎉`;
  fireworks.classList.add("show");

  try {
    tg?.HapticFeedback?.notificationOccurred("success");
  } catch (_) {
    // Не критично.
  }

  setTimeout(() => {
    fireworks.classList.remove("show");
  }, 2200);
}


// =========================
// ROTATION HELPERS
// =========================

function normalizeAngle(angle) {
  return ((angle % 360) + 360) % 360;
}


function normalizeSectorIndex(value) {
  const numericValue = Number(value);

  if (
    Number.isInteger(numericValue) &&
    numericValue >= 0 &&
    numericValue < sectors.length
  ) {
    return numericValue;
  }

  return null;
}


function getSectorIndex(prize, backendSectorIndex) {
  const normalized = normalizeSectorIndex(backendSectorIndex);

  if (normalized !== null) {
    return normalized;
  }

  const byPrize = sectors.indexOf(prize);

  if (byPrize !== -1) {
    return byPrize;
  }

  return FALLBACK_SECTOR_INDEX;
}


// =========================
// NON-SPIN BACKEND RESPONSES
// =========================

function handleRejectedSpin(data) {
  spinning = false;

  if (isFundDepleted(data)) {
    showClosedState(
      data.message ||
      "Подарунки в Колесі Фортуни вже закінчилися."
    );
    return;
  }

  if (data.client_error) {
    setResult(
      data.message || "Помилка з’єднання. Спробуй ще раз.",
      "error"
    );

    setButtonLoading(false);
    return;
  }

  const message =
    data.message ||
    "Ця прокрутка зараз недоступна. Відкрий колесо через Telegram-бота.";

  if (data.prize === "Помилка" || data.http_ok === false) {
    setResult(message, "error");
  } else {
    setResult(message, "repeat");
  }

  // Для backend-відмови не дозволяємо безкінечно тиснути кнопку.
  // Користувач має повернутися в Telegram-бот і виконати потрібну дію:
  // пройти реєстрацію, підписатися або дочекатися cooldown.
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Недоступно";
  }

  setAppStatus("Відкрий Telegram-бот, щоб продовжити.");

  try {
    tg?.HapticFeedback?.notificationOccurred("warning");
  } catch (_) {
    // Не критично.
  }
}


// =========================
// FINISH SUCCESSFUL SPIN
// =========================

function finishSpin(prize, sectorIndex) {
  if (isPrankText(prize)) {
    setResult(prize, "prank");
  } else if (prize === "Помилка") {
    setResult(
      "Помилка. Спробуй відкрити колесо ще раз через Telegram-бота.",
      "error"
    );
  } else if (isTechnicalNoPrize(prize)) {
    setResult(
      "Цього разу без подарунка.",
      "empty"
    );
  } else if (isRealWin(prize)) {
    setResult(`🎉 Вітаємо! Ви виграли: ${prize}`, "win");
    showFireworks(prize, sectorIndex);
  } else {
    setResult(
      "Не вдалося визначити результат прокрутки.",
      "error"
    );
  }

  spinning = false;

  // Успішна реальна прокрутка вже записана backend.
  // Повторно тиснути кнопку в цьому WebApp не потрібно.
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Прокрутку завершено";
  }
}


// =========================
// ANIMATION
// =========================

function animateToSector(prize, sectorIndex) {
  if (!pointerRotator) {
    finishSpin(prize, sectorIndex);
    return;
  }

  const targetAngle =
    sectorIndex * SECTOR_ANGLE +
    SAFE_CENTER_OFFSET +
    POINTER_OFFSET;

  const extraSpins = 5;
  const baseRotation = normalizeAngle(currentRotation);

  let delta = normalizeAngle(targetAngle) - baseRotation;

  if (delta < 0) {
    delta += 360;
  }

  const finalDeg = Math.round(
    currentRotation +
    extraSpins * 360 +
    delta
  );

  currentRotation = finalDeg;

  pointerRotator.style.transition = "none";
  pointerRotator.style.transform =
    `rotate(${Math.round(baseRotation)}deg) translateZ(0)`;

  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      pointerRotator.style.transition =
        "transform 4.2s cubic-bezier(0.16, 1, 0.3, 1)";

      pointerRotator.style.transform =
        `rotate(${finalDeg}deg) translateZ(0)`;
    });
  });

  const onEnd = (event) => {
    if (event.target !== pointerRotator) return;

    pointerRotator.removeEventListener("transitionend", onEnd);

    if (transitionFallbackTimer) {
      clearTimeout(transitionFallbackTimer);
      transitionFallbackTimer = null;
    }

    finishSpin(prize, sectorIndex);
  };

  pointerRotator.addEventListener("transitionend", onEnd);

  transitionFallbackTimer = setTimeout(() => {
    pointerRotator.removeEventListener("transitionend", onEnd);
    transitionFallbackTimer = null;

    finishSpin(prize, sectorIndex);
  }, 5200);
}


// =========================
// SPIN BUTTON
// =========================

if (!btn || !pointerRotator) {
  console.error(
    "Не знайдено spinBtn або pointer-rotator у static/index.html."
  );
} else {
  showWheelState();

  btn.addEventListener("click", async () => {
    if (spinning || wheelClosed) return;

    const telegramData = getTelegramUserData();

    if (!telegramData.user_id) {
      setResult(
        "Відкрий колесо через Telegram-бота, щоб ми могли визначити твій акаунт.",
        "error"
      );

      btn.disabled = true;
      btn.textContent = "Відкрий через Telegram";

      return;
    }

    spinning = true;
    setButtonLoading(true);
    setAppStatus("");
    setResult("Перевіряємо участь...", "default");

    stopFireworks();

    if (transitionFallbackTimer) {
      clearTimeout(transitionFallbackTimer);
      transitionFallbackTimer = null;
    }

    const data = await spinRequest({
      username: telegramData.username,
      user_id: telegramData.user_id,
      initData: telegramData.initData
    });

    // КРИТИЧНО:
    // repeat=true означає, що backend НЕ створив нову прокрутку.
    // У такому випадку колесо взагалі не повинно анімуватися.
    if (data.repeat === true) {
      handleRejectedSpin(data);
      return;
    }

    const prize = String(data.prize || "");
    const sectorIndex = getSectorIndex(
      prize,
      data.sector_index
    );

    // Навіть якщо backend випадково повернув технічний результат
    // з repeat=false, не показуємо фейкову анімацію на сектор подарунка.
    if (
      prize === "Помилка" ||
      isTechnicalNoPrize(prize)
    ) {
      spinning = false;

      if (isFundDepleted(data)) {
        showClosedState(data.message);
        return;
      }

      if (prize === "Помилка") {
        setResult(
          data.message ||
          "Сталася помилка. Спробуй відкрити колесо ще раз.",
          "error"
        );
      } else {
        setResult(
          data.message || "Цього разу без подарунка.",
          "empty"
        );
      }

      if (btn) {
        btn.disabled = true;
        btn.textContent = "Прокрутку завершено";
      }

      return;
    }

    setResult("Крутимо...", "default");

    try {
      tg?.HapticFeedback?.impactOccurred("medium");
    } catch (_) {
      // Не критично.
    }

    animateToSector(prize, sectorIndex);
  });
}