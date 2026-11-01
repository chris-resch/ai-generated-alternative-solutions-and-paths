# Who authored the merged solution

```
Issues
  in the benchmark: 500; with at least two passing agent patches: 419
  with a passing agent patch: 441
  ... and the commit that merged the solution found: 426
  ... and at least one solving run (not stuck, edits a source file): 426
  ... and an author who is not a bot: 426
  distinct authors: 259; authors in the core: 247; candidates per issue: median 267, 92994 rows in all
  issues whose author had no commit in the year before: 128

Paths
  solving runs (passing, edit a source file, not stuck): 3847 in 441 issues
  files solving required per issue (the issues above): median 4, of which the merged solution did not change: median 3
  issues with a file read by at least half of the solving runs outside the merged solution: 217
  issues whose solving reached at least one directory beyond the changed code (reach >= 1): 155

Table A1. Who authored the merged solution (conditional logit; outcome: 1 for the author among the issue's candidates)
                                                    (1)            (2)            (3)            (4)      
Commits in the year before (activity)             0.762***       0.672***       0.332**        0.331**    
                                                  (0.096)        (0.106)        (0.132)        (0.132)    
Past commits: files the issue names                              0.358***        -0.062         -0.060    
                                                                 (0.096)        (0.091)        (0.090)    
Past commits: files solving required                                            0.860***       0.935***   
                                                                                (0.146)        (0.179)    
Past commits: files the merged solution changed                                                  -0.098    
                                                                                               (0.175)    
Issues                                              426            426            426            426      
Issue x candidate rows                             92994          92994          92994          92994     
Log-likelihood                                    -1984.4        -1971.1        -1905.6        -1905.3    
Pseudo R2 (vs. a random pick)                      0.118          0.124          0.154          0.154     
  Standard errors clustered by author in parentheses. * p<0.10, ** p<0.05, *** p<0.01.

Table A2. Whether the author of the merged solution is a core member (logit, one row per issue)
                                                    (1)            (2)            (3)      
Reach (directories read beyond those changed)     0.322**        0.315**        0.330**    
                                                  (0.137)        (0.139)        (0.149)    
Solution size (log(1 + source files changed))                     0.304          0.258     
                                                                 (0.638)        (0.625)    
Constant                                           0.165          -0.058         0.384     
                                                  (0.117)        (0.481)        (0.713)    
Project fixed effects                                no             no            yes      
Issues                                              426            426            423      
Log-likelihood                                     -286.6         -286.4         -269.9    
Pseudo R2                                          0.011          0.012          0.062     
  Robust standard errors in parentheses. * p<0.10, ** p<0.05, *** p<0.01. Column (3) leaves out 2 projects whose authors are all core or all not core.
  Core share by reach: 0: 53% of 271, 1: 65% of 112, 2 or more: 70% of 43
```
