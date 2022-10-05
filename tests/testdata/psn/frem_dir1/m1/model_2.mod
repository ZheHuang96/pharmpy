$PROBLEM    PHENOBARB SIMPLE MODEL
$DATA      ../frem_dataset.dta IGNORE=@
$INPUT      ID TIME AMT WGT APGR DV MDV FREMTYPE
$SUBROUTINE ADVAN1 TRANS2
$PK
CL=THETA(1)*EXP(ETA(1))
V=THETA(2)*EXP(ETA(2))
S1=V

    SDC3 = 2.23763568135
    SDC4 = 0.704564727537
$ERROR
Y=F+F*EPS(1)

;;;FREM CODE BEGIN COMPACT
;;;DO NOT MODIFY
    IF (FREMTYPE.EQ.100) THEN
;      APGR  2.23763568135
       Y = THETA(3) + ETA(3)*SDC3 + EPS(2)
       IPRED = THETA(3) + ETA(3)*SDC3
    END IF
    IF (FREMTYPE.EQ.200) THEN
;      WGT  0.704564727537
       Y = THETA(4) + ETA(4)*SDC4 + EPS(2)
       IPRED = THETA(4) + ETA(4)*SDC4
    END IF
;;;FREM CODE END COMPACT
$THETA  (0,0.00581756) FIX ; TVCL
$THETA  (0,1.44555) FIX ; TVV
$THETA  6.42372881356 FIX ; TV_APGR
 1.52542372881 FIX ; TV_WGT
$OMEGA  0.111053  FIX  ;       IVCL
$OMEGA  0.201526  FIX  ;        IVV
$OMEGA  BLOCK(2)
 1  ;   BSV_APGR
 0.244578970875 1  ;    BSV_WGT
$SIGMA  0.0164177  FIX
$SIGMA  0.0000001  FIX  ;     EPSCOV
$ESTIMATION METHOD=1 INTERACTION NONINFETA=1 MCETA=1 MAXEVALS=0
$COVARIANCE
$ETAS       FILE=model_2_input.phi
