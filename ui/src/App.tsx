import type { ReactNode } from "react";
import { Link, Navigate, Route, Routes, useLocation, useNavigate } from "react-router";

import { clearSession, getSession } from "./auth";
import DetailPage from "./pages/DetailPage";
import LoginPage from "./pages/LoginPage";
import QueuePage from "./pages/QueuePage";

function RequireLogin({ children }: { children: ReactNode }) {
  return getSession() ? children : <Navigate to="/login" replace />;
}

export default function App() {
  useLocation(); // re-render on every page change, so the top bar shows the current login
  const navigate = useNavigate();
  const session = getSession();

  function logout() {
    clearSession();
    navigate("/login");
  }

  return (
    <div className="app">
      <header className="topbar">
        <Link to="/" className="brand">
          Alpentech · Order Review
        </Link>
        {session && (
          <span className="user">
            {session.name}
            <button onClick={logout}>Sign out</button>
          </span>
        )}
      </header>
      <main className="content">
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/" element={<RequireLogin><QueuePage /></RequireLogin>} />
          <Route path="/reviews/:key" element={<RequireLogin><DetailPage /></RequireLogin>} />
        </Routes>
      </main>
    </div>
  );
}
