import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./app.css";

const themeParam = new URLSearchParams(window.location.search).get("theme");
if (themeParam === "dark" || themeParam === "light") localStorage.setItem("sopilot-theme", themeParam);
const saved = localStorage.getItem("sopilot-theme");
const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
if (saved === "dark" || (!saved && prefersDark)) document.documentElement.classList.add("dark");

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
