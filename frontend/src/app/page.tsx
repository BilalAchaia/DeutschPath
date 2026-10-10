"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function Home() {
  const router = useRouter();

  useEffect(() => {
    router.replace("/reader/");
  }, [router]);

  return (
    <div className="flex min-h-[60vh] items-center justify-center text-sm text-slate-400">
      Loading…
    </div>
  );
}
