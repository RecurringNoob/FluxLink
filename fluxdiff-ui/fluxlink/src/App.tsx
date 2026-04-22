import { Routes, Route } from "react-router";
import FluxDiff from "./pages/FluxDiffPage";
import ChatPage from "./pages/ChatPage";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<FluxDiff />} />
      <Route path="/chat" element={<ChatPage />} />
    </Routes>
  );
}