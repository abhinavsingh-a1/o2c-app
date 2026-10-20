import { Link, Route, Routes } from "react-router";

import DetailPage from "./pages/DetailPage";
import QueuePage from "./pages/QueuePage";

export default function App() {
  return (
    <div className="app">
      <header className="topbar">
        <Link to="/" className="brand">
          Alpentech · Order Review
        </Link>
      </header>
      <main className="content">
        <Routes>
          <Route path="/" element={<QueuePage />} />
          <Route path="/reviews/:key" element={<DetailPage />} />
        </Routes>
      </main>
    </div>
  );
}
