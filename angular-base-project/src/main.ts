import { bootstrapApplication } from '@angular/platform-browser';
import { AppComponent } from './app/app.component';
import { appConfig } from './app/app.config';
import './screen-runtime';

// Auto-bootstrap for development (ng serve)
bootstrapApplication(AppComponent, appConfig)
  .then((appRef) => {
    (window as any).__SCREEN_REF__ = appRef;
    if ((window as any).__AIQOD_SCREEN__) {
      (window as any).__AIQOD_SCREEN__.setScreenId("screen_20260706131336_0443feac");
    }
  })
  .catch((err) => console.error('Bootstrap error:', err));

// Export bootstrap function for DOM-based workflow loading (bracket key: screen ids may contain hyphens)
(window as any)["bootstrap_screen_20260706131336_0443feac"] = () => {
  if ((window as any).__SCREEN_REF__) {
    if ((window as any).__AIQOD_SCREEN__) {
      (window as any).__AIQOD_SCREEN__.setScreenId("screen_20260706131336_0443feac");
    }
    return Promise.resolve((window as any).__SCREEN_REF__);
  }

  const screenRoot = document.getElementById('screen-root');
  if (screenRoot && !screenRoot.querySelector('app-project-ai-recommendation')) {
    const componentElement = document.createElement('app-project-ai-recommendation');
    screenRoot.appendChild(componentElement);
  }

  return bootstrapApplication(AppComponent, appConfig)
    .then((appRef) => {
      (window as any).__SCREEN_REF__ = appRef;
      if ((window as any).__AIQOD_SCREEN__) {
        (window as any).__AIQOD_SCREEN__.setScreenId("screen_20260706131336_0443feac");
      }
      return appRef;
    })
    .catch((err) => {
      console.error('Bootstrap error:', err);
      throw err;
    });
};
