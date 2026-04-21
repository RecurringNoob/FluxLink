const pcbDiffStyles = `
.pcb-diff-viewer {
  display: grid;
  grid-template-columns: 1fr 1fr;
  grid-template-rows: 1fr 1fr;
  gap: 24px;
  background: #181a1b;
  padding: 32px;
  min-height: 100vh;
}

.pcb-panel {
  background: #232526;
  border-radius: 8px;
  box-shadow: 0 2px 12px rgba(0,0,0,0.25);
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 16px;
}

.pcb-panel-title {
  color: #e0e0e0;
  font-size: 1.1rem;
  font-weight: 600;
  margin-bottom: 12px;
  letter-spacing: 0.5px;
  text-align: center;
}

.pcb-panel-image img {
  max-width: 100%;
  max-height: 320px;
  width: auto;
  height: auto;
  border-radius: 4px;
  display: block;
  margin: 0 auto;
  background: #272b2f;
  box-shadow: 0 0 0 1px #333;
}

/* Responsive: stack vertically on small screens */
@media (max-width: 900px) {
  .pcb-diff-viewer {
    grid-template-columns: 1fr;
    grid-template-rows: repeat(4, auto);
    gap: 16px;
    padding: 16px;
  }
}
`;

if (typeof document !== "undefined") {
  const styleTag = document.createElement("style");
  styleTag.innerHTML = pcbDiffStyles;
  document.head.appendChild(styleTag);
}