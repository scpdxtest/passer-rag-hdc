import './App.css';
import "primereact/resources/themes/lara-light-teal/theme.css";
import "primereact/resources/primereact.min.css";
import "primeicons/primeicons.css";
import { Routes, Route } from 'react-router-dom';
import Navigation from './component/Nav';
import About from './component/About';
import ErrorBoundary from './component/ErrorBoundry';
import SelectModel from './component/SelectModel';
import AddModel from './component/addModel';
import TestWharf from './component/TestWharf';
import ManageDB from './component/manageDB';
import DBFromCorpusPapers from './component/dbFromCorpusPapers';
import ScorePapersBat from './component/scorePapersBat';
import ChatNewRAG from './component/chatNewRAG';

// stable chromadb lib version: 1.10.4 !!!!!

function App() {
  return (
    <div className="App">
      <ErrorBoundary>
        <Navigation />
        <Routes>
          <Route path='/about' element={<About/>}/>
          <Route path='/' element={<About/>}/>
          <Route path="/dbfromcorpuspapers" element={<DBFromCorpusPapers/>} />
          <Route path="/scorepapersbat" element={<ScorePapersBat/>} />
          <Route path="/chatnewrag" element={<ChatNewRAG/>} />
          <Route path="/selectmodel" element={<SelectModel/>} />
          <Route path="/addmodel" element={<AddModel/>} />
          <Route path="/managedb" element={<ManageDB/>} />
          <Route path="/testwharf" element={<TestWharf/>} />
        </Routes>
      </ErrorBoundary>
    </div>
  );
}

export default App;
