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

let spinning = false;
let currentRotation = 0;
let transitionFallbackTimer = null;

// ПОРЯДОК СЕКТОРІВ = ЯК НА НОВОМУ КОЛЕСІ
// ВІД ВЕРХУ ЗА ГОДИННИКОВОЮ:
// 0 — Лоторейка OXVA
// 1 — Шопер
// 2 — Нічого
// 3 — Головний убір
// 4 — OXVA Go Lite
// 5 — OXVA Pro 3
// 6 — Брелок
const sectors = [
  "Лоторейка OXVA",
  "Шопер",
  "Нічого",
  "Головний убір",
  "OXVA Go Lite",
  "OXVA Pro 3",
  "Брелок"
];

const SECTOR_ANGLE = 360 / sectors.length;

// На новому колесі сектор 0 вже стоїть по центру зверху,
// тому половину сектора більше не додаємо.
const SAFE_CENTER_OFFSET = 0;

// Мікрокалібрування стрілки.
// 0 = рівно по центру сектора.
// Якщо стрілка трохи вліво/вправо — міняємо це число.
// Наприклад: -3, +3, -5, +5.
const POINTER_OFFSET = 0;

const NOTHING_SECTOR_INDEX = 2;

function setResult(text, type = "default") {
  if (!res) return;

  res.textContent = text;

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

async function spinRequest(payload) {
  try {
    const r = await fetch("/spin", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    return await r.json();
  } catch (e) {
    console.error(e);

    return {
      prize: "Помилка",
      sector_index: NOTHING_SECTOR_INDEX,
      repeat: true,
      message: "Помилка. Спробуй ще раз пізніше."
    };
  }
}

function isPrankText(text) {
  return typeof text === "string" && text.toLowerCase().includes("попався");
}

function isRealWin(prize, sectorIndex) {
  if (!prize) return false;
  if (prize === "Нічого") return false;
  if (prize === "Помилка") return false;
  if (sectorIndex === NOTHING_SECTOR_INDEX) return false;
  if (isPrankText(prize)) return false;

  return true;
}

function showFireworks(text, sectorIndex) {
  if (!fireworks || !fireworksText) return;

  if (!isRealWin(text, sectorIndex)) {
    return;
  }

  fireworksText.textContent = `🎉 ${text} 🎉`;
  fireworks.classList.add("show");

  setTimeout(() => {
    fireworks.classList.remove("show");
  }, 2200);
}

function normalizeAngle(angle) {
  return ((angle % 360) + 360) % 360;
}

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

function finishSpin(prize, sectorIndex, repeat, message) {
  if (repeat) {
    setResult(message || "Ви вже крутили колесо.", "repeat");
  } else if (isPrankText(prize)) {
    setResult(prize, "prank");
  } else if (prize === "Нічого") {
    setResult(
      "На жаль, цього разу без подарунка. Спробуй наступного разу!",
      "empty"
    );
  } else if (prize === "Помилка") {
    setResult("Помилка. Спробуй ще раз пізніше.", "error");
  } else {
    setResult(`🎉 Вітаємо! Ви виграли: ${prize}`, "win");
  }

  showFireworks(prize, sectorIndex);

  spinning = false;

  if (btn) {
    btn.disabled = false;
  }
}

if (!btn || !pointerRotator) {
  console.error("Не знайдено spinBtn або pointer-rotator у HTML.");
} else {
  btn.addEventListener("click", async () => {
    if (spinning) return;

    spinning = true;
    btn.disabled = true;
    setResult("Крутимо...", "default");

    if (transitionFallbackTimer) {
      clearTimeout(transitionFallbackTimer);
      transitionFallbackTimer = null;
    }

    const telegramData = getTelegramUserData();

    const data = await spinRequest({
      username: telegramData.username,
      user_id: telegramData.user_id,
      initData: telegramData.initData
    });

    const { prize, sector_index, repeat, message } = data;

    let sectorIndex = NOTHING_SECTOR_INDEX;

    if (typeof sector_index === "number" && sector_index >= 0) {
      sectorIndex = sector_index % sectors.length;
    } else {
      const idx = sectors.indexOf(prize);
      sectorIndex = idx !== -1 ? idx : NOTHING_SECTOR_INDEX;
      console.warn("Prize not matched, using fallback sector:", prize);
    }

    const targetAngle =
      sectorIndex * SECTOR_ANGLE + SAFE_CENTER_OFFSET + POINTER_OFFSET;

    const extraSpins = 5;
    const baseRotation = normalizeAngle(currentRotation);

    let delta = normalizeAngle(targetAngle) - baseRotation;
    if (delta < 0) delta += 360;

    const finalDeg = Math.round(currentRotation + extraSpins * 360 + delta);
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

    const onEnd = (e) => {
      if (e.target !== pointerRotator) return;

      pointerRotator.removeEventListener("transitionend", onEnd);

      if (transitionFallbackTimer) {
        clearTimeout(transitionFallbackTimer);
        transitionFallbackTimer = null;
      }

      finishSpin(prize, sectorIndex, repeat, message);
    };

    pointerRotator.addEventListener("transitionend", onEnd);

    transitionFallbackTimer = setTimeout(() => {
      pointerRotator.removeEventListener("transitionend", onEnd);
      transitionFallbackTimer = null;

      finishSpin(prize, sectorIndex, repeat, message);
    }, 5200);
  });
}