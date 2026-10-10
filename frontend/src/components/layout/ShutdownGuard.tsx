"use client";

import { useEffect } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

function isLocalhost(): boolean {
  if (typeof window === "undefined") return false;
  return ["localhost", "127.0.0.1", "::1", "[::1]"].includes(window.location.hostname);
}

export function ShutdownGuard() {
  useEffect(() => {
    // Only relevant when running locally (the launcher starts real servers on
    // this machine). In the cloud deployment the service must stay up, so we
    // never fire the shutdown beacon there.
    if (!isLocalhost()) return;

    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };

    const onPageHide = () => {
      navigator.sendBeacon(`${API}/shutdown`);
    };

    window.addEventListener("beforeunload", onBeforeUnload);
    window.addEventListener("pagehide", onPageHide);

    return () => {
      window.removeEventListener("beforeunload", onBeforeUnload);
      window.removeEventListener("pagehide", onPageHide);
    };
  }, []);

  return null;
}
