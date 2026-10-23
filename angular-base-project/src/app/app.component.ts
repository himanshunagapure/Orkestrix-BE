import { Component, OnInit, signal, inject, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ApiService } from './services/api.service';
import { 
  FetchProjectDataResponse, 
  GenerateRecommendationsResponse, 
  SubmitRecommendationsRequest,
  SubmitRecommendationsResponse
} from './models/api.models';
import { catchError, switchMap, EMPTY, of } from 'rxjs';
import AOS from 'aos';

@Component({
  selector: 'app-project-ai-recommendation',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './app.component.html',
  styleUrls: ['./app.component.css'],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class AppComponent implements OnInit {
  private apiService = inject(ApiService);

  // State Signals
  loading = signal<boolean>(false);
  loadingMessage = signal<string>('');
  error = signal<string | null>(null);
  
  // Input Data
  selectedProjectId = signal<string | null>(null);
  geminiKey = signal<string | null>(null);
  
  // Recommendation Data
  recommendations = signal<GenerateRecommendationsResponse | null>(null);

  ngOnInit() {
    // Initialize animations
    AOS.init({
      duration: 600,
      once: true,
      easing: 'ease-out-cubic'
    });

    // Bind to workflow inputs
    if ((window as any).__AIQOD_SCREEN__) {
      (window as any).__AIQOD_SCREEN__.onInput((inputData: any) => {
        if (inputData) {
          if (inputData.selected_project_id) {
            this.selectedProjectId.set(inputData.selected_project_id);
          }
          if (inputData.Gemini_Key) {
            this.geminiKey.set(inputData.Gemini_Key);
          }
          
          // Start the flow if we have the required project ID
          if (this.selectedProjectId()) {
            this.startRecommendationFlow();
          } else {
            this.error.set('Missing required input: selected_project_id');
          }
        }
      });
    } else {
      // Fallback for local testing if needed
      this.error.set('Workflow context not found. Cannot proceed.');
    }
  }

  private startRecommendationFlow() {
    const projectId = this.selectedProjectId();
    if (!projectId) return;

    this.loading.set(true);
    this.loadingMessage.set('Fetching historical project data...');
    this.error.set(null);

    this.apiService.callFunction<FetchProjectDataResponse>('fetch_project_data', {
      selected_project_id: projectId
    }).pipe(
      switchMap(res => {
        if (res.error) {
          throw new Error(res.error);
        }
        if (!res.project_data) {
          throw new Error('No historical project data found for the selected ID.');
        }

        this.loadingMessage.set('Generating AI recommendations, please wait...');
        
        // Call Gemini generation
        return this.apiService.callFunction<GenerateRecommendationsResponse>('generate_ai_recommendations', {
          project_data: res.project_data,
          gemini_key: this.geminiKey() || '' // Pass empty string if not provided, backend handles fallback
        });
      }),
      catchError(err => {
        this.error.set(err.message || 'An unexpected error occurred during the recommendation process.');
        this.loading.set(false);
        return EMPTY;
      })
    ).subscribe({
      next: (res) => {
        if (res.error) {
          this.error.set(res.error);
        } else {
          this.recommendations.set(res);
        }
        this.loading.set(false);
      }
    });
  }

  onContinue() {
    const recs = this.recommendations();
    if (!recs) return;

    this.loading.set(true);
    this.loadingMessage.set('Submitting recommendations...');
    this.error.set(null);

    // Prepare payload matching SubmitRecommendationsRequest model
    const payload: SubmitRecommendationsRequest = {
      recommended_project_plan: recs.recommended_project_plan || '',
      plan_confidence_score: recs.plan_confidence_score || 0,
      recommended_resource_structure: recs.recommended_resource_structure || '',
      resource_confidence_score: recs.resource_confidence_score || 0,
      recommended_risk_template: recs.recommended_risk_template || '',
      risk_confidence_score: recs.risk_confidence_score || 0,
      suggested_timeline: recs.suggested_timeline || '',
      estimated_budget: recs.estimated_budget || 0,
      suggested_team_size: recs.suggested_team_size || 0
    };

    this.apiService.callFunction<SubmitRecommendationsResponse>('submit_recommendations', payload)
      .pipe(
        catchError(err => {
          this.error.set(err.message || 'Failed to submit recommendations.');
          this.loading.set(false);
          return of(null);
        })
      )
      .subscribe(res => {
        if (res) {
          if (res.error) {
            this.error.set(res.error);
            this.loading.set(false);
          } else {
            // Success - Complete workflow with output parameters
            this.loading.set(false);
            if ((window as any).__AIQOD_SCREEN__) {
              // Output matches the required outputParameters schema
              (window as any).__AIQOD_SCREEN__.complete(payload);
            }
          }
        }
      });
  }
}
